/** @vitest-environment jsdom */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "../api";
import { ProvidersPage } from "./ProvidersPage";
vi.mock("../api", async () => {
 const actual = await vi.importActual<typeof import("../api")>("../api");
 return { ...actual, getProviders: vi.fn(), setLunaEnabled: vi.fn() };
});
describe("ProvidersPage", () => {
 beforeEach(() => { cleanup(); vi.resetAllMocks(); });
 it("shows readiness, cloud disclosure, and toggle action", async () => {
  vi.mocked(api.getProviders).mockResolvedValue({providers:[
   {id:"local_qwen",model:"ddu-narrator",configured:true,enabled:true,ready:true,failures:0,requests:3,last_failure:null},
   {id:"codex_cli",model:"gpt-6-luna",configured:true,enabled:false,ready:true,failures:1,requests:4,last_failure:"timeout"}
  ].map((provider) => ({
   ...provider,
   last_health_check:"2026-11-17T00:00:00Z",
   health_latency_ms:20,
   last_latency_ms:150,
   latency_samples:3,
   total_latency_ms:450,
   error_class:provider.id === "codex_cli" ? "timeout" : null,
  }))} as unknown as api.ProviderSnapshot);
  vi.mocked(api.setLunaEnabled).mockResolvedValue({enabled:true});
  render(<ProvidersPage csrfToken="csrf-test" onSessionExpired={() => {}} />);
  expect(await screen.findByText("Local Qwen")).toBeTruthy();
  expect(screen.getAllByText("150 ms").length).toBe(2);
  expect(screen.getAllByText("20 ms").length).toBe(2);
  expect(screen.getByText("timeout")).toBeTruthy();
  expect(screen.getByText(/sends prompts and supplied conversation context to OpenAI/i)).toBeTruthy();
  const enable = screen.getByRole("button",{name:"Enable GPT-6 Luna"}) as HTMLButtonElement;
  expect(enable.disabled).toBe(true);
  fireEvent.click(screen.getByRole("checkbox"));
  fireEvent.click(enable);
  await waitFor(() => expect(api.setLunaEnabled).toHaveBeenCalledWith(true,"csrf-test"));
 });
 it("shows gateway unavailable state", async () => {
  vi.mocked(api.getProviders).mockRejectedValue(new api.ApiRequestError(503));
  render(<ProvidersPage csrfToken="csrf-test" onSessionExpired={() => {}} />);
  expect((await screen.findByRole("alert")).textContent).toContain("Inference gateway is not configured or is offline.");
 });
 it("expires the dashboard session when a provider toggle returns 401", async () => {
  const expired = vi.fn();
  vi.mocked(api.getProviders).mockResolvedValue({providers:[
   {id:"local_qwen",model:"ddu-narrator",configured:true,enabled:true,ready:true,failures:0,requests:3,last_failure:null},
   {id:"codex_cli",model:"gpt-6-luna",configured:true,enabled:false,ready:true,failures:0,requests:0,last_failure:null}
  ].map((provider) => ({
   ...provider,
   last_health_check:null,
   health_latency_ms:null,
   last_latency_ms:null,
   latency_samples:0,
   total_latency_ms:0,
   error_class:null,
  }))} as unknown as api.ProviderSnapshot);
  vi.mocked(api.setLunaEnabled).mockRejectedValue(new api.ApiRequestError(401));
  render(<ProvidersPage csrfToken="csrf-test" onSessionExpired={expired} />);
  fireEvent.click(await screen.findByRole("checkbox"));
  fireEvent.click(screen.getByRole("button",{name:"Enable GPT-6 Luna"}));
  await waitFor(() => expect(expired).toHaveBeenCalledOnce());
 });
});
