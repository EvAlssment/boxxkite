package com.boxxkite;

import java.util.concurrent.CompletableFuture;

import static com.boxxkite.Models.*;

/** Client facade bound to a caller-supplied sandbox session ID. */
public final class Session {
    private final Client client;
    private final String id;

    Session(Client client, String id) { this.client = client; this.id = id; }

    public String id() { return id; }

    public ExecResult exec(String command) { return client.exec(id, command); }
    public ExecResult exec(String command, Integer timeoutSeconds) { return client.exec(id, command, timeoutSeconds); }
    public CompletableFuture<ExecResult> execAsync(String command) { return client.execAsync(id, command); }
    public CompletableFuture<ExecResult> execAsync(String command, Integer timeoutSeconds) { return client.execAsync(id, command, timeoutSeconds); }
    public FileCreateResult fileCreate(String path, String content) { return client.fileCreate(id, path, content); }
    public CompletableFuture<FileCreateResult> fileCreateAsync(String path, String content) { return client.fileCreateAsync(id, path, content); }
    public FileViewResult view(String path) { return client.view(id, path); }
    public CompletableFuture<FileViewResult> viewAsync(String path) { return client.viewAsync(id, path); }
    public ReplaceResult strReplace(String path, String oldText, String newText, boolean replaceAll) {
        return client.strReplace(id, path, oldText, newText, replaceAll);
    }
    public CompletableFuture<ReplaceResult> strReplaceAsync(String path, String oldText, String newText, boolean replaceAll) {
        return client.strReplaceAsync(id, path, oldText, newText, replaceAll);
    }
    public DirectoryListResult ls(String path) { return client.ls(id, path); }
    public CompletableFuture<DirectoryListResult> lsAsync(String path) { return client.lsAsync(id, path); }
    public GlobResult glob(String pattern, String path) { return client.glob(id, pattern, path); }
    public CompletableFuture<GlobResult> globAsync(String pattern, String path) { return client.globAsync(id, pattern, path); }
    public GrepResult grep(String pattern, String path) { return client.grep(id, pattern, path); }
    public CompletableFuture<GrepResult> grepAsync(String pattern, String path) { return client.grepAsync(id, pattern, path); }
    public ProcessStartResult startProcess(String command) { return client.startProcess(id, command); }
    public CompletableFuture<ProcessStartResult> startProcessAsync(String command) { return client.startProcessAsync(id, command); }
    public ProcessListResult listProcesses() { return client.listProcesses(id); }
    public CompletableFuture<ProcessListResult> listProcessesAsync() { return client.listProcessesAsync(id); }
    public ProcessOutputResult getProcessOutput(String processId, long sinceOffset) { return client.getProcessOutput(id, processId, sinceOffset); }
    public CompletableFuture<ProcessOutputResult> getProcessOutputAsync(String processId, long sinceOffset) {
        return client.getProcessOutputAsync(id, processId, sinceOffset);
    }
    public ProcessInputResult sendProcessInput(String processId, String data) { return client.sendProcessInput(id, processId, data); }
    public CompletableFuture<ProcessInputResult> sendProcessInputAsync(String processId, String data) {
        return client.sendProcessInputAsync(id, processId, data);
    }
    public ProcessStopResult stopProcess(String processId) { return client.stopProcess(id, processId); }
    public CompletableFuture<ProcessStopResult> stopProcessAsync(String processId) { return client.stopProcessAsync(id, processId); }
}
