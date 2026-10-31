package com.boxxkite.examples;

import com.boxxkite.Client;
import com.boxxkite.Models.CreateSandboxRequest;
import com.boxxkite.Models.ExecResult;
import com.boxxkite.Models.Sandbox;
import com.boxxkite.Session;

/** Runnable example; set BOXXKITE_BASE_URL and BOXXKITE_API_KEY first. */
public final class Quickstart {
    private Quickstart() {}

    public static void main(String[] args) {
        String baseUrl = requiredEnvironment("BOXXKITE_BASE_URL");
        String apiKey = requiredEnvironment("BOXXKITE_API_KEY");
        Client client = new Client(baseUrl, apiKey);
        Sandbox sandbox = client.createSandbox(new CreateSandboxRequest("java-sdk-example"));
        try {
            Session session = client.session(sandbox.id);
            ExecResult result = session.exec("python3 -c 'print(1 + 1)'");
            System.out.print(result.stdout);
        } finally {
            client.destroySandbox(sandbox.id);
        }
    }

    private static String requiredEnvironment(String name) {
        String value = System.getenv(name);
        if (value == null || value.isBlank()) throw new IllegalStateException("Set the " + name + " environment variable");
        return value;
    }
}
