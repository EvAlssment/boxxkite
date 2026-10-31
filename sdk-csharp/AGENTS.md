# sdk-csharp

This is a hand-maintained wrapper over the existing Boxxkite control-plane
REST API. Keep requests and response models aligned with
`control-plane/src/control_plane/schemas.py` and the route implementations
in `control-plane/src/control_plane/routers/`; there is no OpenAPI codegen.

## Build and test

```bash
dotnet build Boxxkite.Client.sln --configuration Release
dotnet test Boxxkite.Client.sln --configuration Release
```

Tests use a real loopback HTTP listener. Keep credentials out of examples and
logs, and preserve the rule that non-loopback endpoints require HTTPS.

## Scope

Do not imply parity with other SDKs. Add focused request/response tests for
every endpoint introduced, including cancellation and structured API errors.
