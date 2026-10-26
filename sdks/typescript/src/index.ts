export {
  CLIENT_HEADER,
  DEFAULT_BASE_URL,
  DEFAULT_MAX_RETRIES,
  DEFAULT_TIMEOUT_MS,
  VoiceAgentsClient,
  type ClientOptions,
  type HealthStatus,
} from "./client.js";
export {
  APIConnectionError,
  APIError,
  APITimeoutError,
  AuthenticationError,
  BadRequestError,
  ConflictError,
  InsufficientCreditsError,
  InternalServerError,
  NotFoundError,
  PermissionDeniedError,
  RateLimitError,
  VoiceAgentsError,
  type ParsedErrorBody,
} from "./errors.js";
export type { components, operations, paths } from "./schema.js";
export { VERSION } from "./version.js";
