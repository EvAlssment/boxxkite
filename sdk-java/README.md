# Boxxkite Java Client

Java 11+ client for the Boxxkite hosted or self-hosted control-plane HTTP API.
It uses the JDK `HttpClient`; account API keys are sent as bearer credentials
and are never included in exception messages or logged by this library.

## Install

The package is not published to Maven Central yet. From a checkout, build and
install it locally with `./mvnw install`; then add:

```xml
<dependency>
  <groupId>com.boxxkite</groupId>
  <artifactId>boxxkite-client</artifactId>
  <version>0.1.0-SNAPSHOT</version>
</dependency>
```

## Quickstart

```java
import com.boxxkite.Client;
import com.boxxkite.Models.CreateSandboxRequest;
import com.boxxkite.Models.ExecResult;
import com.boxxkite.Models.Sandbox;
import com.boxxkite.Session;

Client client = new Client("https://your-control-plane.example.com", "bxk_live_...");
Sandbox sandbox = client.createSandbox(new CreateSandboxRequest("java-demo"));
Session session = client.session(sandbox.id); // explicit ID; no hidden session state
ExecResult result = session.exec("python3 -c 'print(1 + 1)'");
System.out.println(result.stdout);
client.destroySandbox(sandbox.id);
```

For async calls, use the matching `Async` method and compose its
`CompletableFuture`, for example:

```java
client.execAsync(sandbox.id, "echo hello")
    .thenAccept(output -> System.out.println(output.stdout))
    .join();
```

## Supported operations

Sandbox create/get/list/destroy; synchronous command execution; file create,
view, replace, directory listing, glob, and grep; and background process
start/list/output/input/stop. Each has a synchronous method and an async
`CompletableFuture` variant. `Client` methods take an explicit session ID;
`client.session(id)` provides a small convenience facade without creating or
destroying the session implicitly.

The route and JSON shapes match the existing Go/Rust clients. This initial
SDK does not claim parity for WebSockets, streaming, previews, LSP, or account-
wide resource APIs; no routes for those features are synthesized here.

Retries are disabled by default. Set `RetryConfig.ofMaxRetries(n)` to enable
exponential backoff with jitter; `429` responses can be retried, while
transient `5xx` and connection failures are retried only for idempotent
methods. `Retry-After` is honored and capped. POST operations are never retried
on transient server errors.

Production control-plane URLs must use HTTPS. Plain HTTP is accepted only for
loopback development servers. `BoxxkiteException` exposes the HTTP status,
machine-readable code, retryable flag, and remediation text where supplied by
the API.

## Build and tests

```bash
./mvnw test
```

The tests use an in-process JDK HTTP server and do not call a live control
plane. After Maven has downloaded its wrapper distribution and dependencies,
`./mvnw -o test` runs offline from the local Maven cache.

Runnable end-to-end example (requires a reachable control plane and API key):

```bash
export BOXXKITE_BASE_URL=https://your-control-plane.example.com
export BOXXKITE_API_KEY=bxk_live_...
./mvnw -q -DskipTests package
./mvnw -q dependency:copy-dependencies -DincludeScope=runtime
java -cp 'target/classes:target/dependency/*' com.boxxkite.examples.Quickstart
```

The example source is `src/main/java/com/boxxkite/examples/Quickstart.java`.
