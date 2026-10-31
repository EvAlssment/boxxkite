package com.boxxkite;

import com.fasterxml.jackson.annotation.JsonInclude;
import com.fasterxml.jackson.annotation.JsonProperty;
import java.util.List;
import java.util.Map;

/** Typed request and response models for the supported v1 API routes. */
public final class Models {
    private Models() {}

    @JsonInclude(JsonInclude.Include.NON_NULL)
    public static final class CreateSandboxRequest {
        public String label;
        public String size;
        @JsonProperty("storage_gb") public Double storageGb;
        @JsonProperty("lifetime_minutes") public Integer lifetimeMinutes;
        public Integer count;
        @JsonProperty("secret_names") public List<String> secretNames;
        @JsonProperty("image_id") public String imageId;
        @JsonProperty("mcp_connection_names") public List<String> mcpConnectionNames;
        @JsonProperty("volume_mounts") public Map<String, String> volumeMounts;
        @JsonProperty("gpu_count") public Integer gpuCount;

        public CreateSandboxRequest() {}
        public CreateSandboxRequest(String label) { this.label = label; }
    }

    public static final class Sandbox {
        public String id;
        public String status;
        public String label;
        @JsonProperty("created_at") public String createdAt;
        @JsonProperty("destroyed_at") public String destroyedAt;
        @JsonProperty("expires_at") public String expiresAt;
        public SandboxConnectInfo connect;
        public UsageSummary usage;
    }

    public static final class SandboxConnectInfo {
        @JsonProperty("pod_name") public String podName;
        public String note;
    }

    public static final class UsageSummary {
        @JsonProperty("monthly_sandbox_hours_used") public double monthlySandboxHoursUsed;
        @JsonProperty("monthly_sandbox_hours_limit") public double monthlySandboxHoursLimit;
        @JsonProperty("concurrent_sandboxes") public int concurrentSandboxes;
        @JsonProperty("concurrent_sandboxes_limit") public int concurrentSandboxesLimit;
        @JsonProperty("total_sandboxes_created") public int totalSandboxesCreated;
    }

    public static final class ExecResult {
        @JsonProperty("exit_code") public int exitCode;
        public String stdout;
        public String stderr;
    }

    public static final class FileCreateResult {
        public String path;
        public int size;
        public boolean created;
    }

    public static final class FileViewResult {
        public String content;
        public int lines;
        @JsonProperty("is_directory") public boolean isDirectory;
        public List<DirectoryEntry> entries;
    }

    public static final class DirectoryEntry {
        public String path;
        @JsonProperty("is_dir") public boolean isDirectory;
        public long size;
    }

    public static final class ReplaceResult {
        public String path;
        public boolean replaced;
        public int occurrences;
    }

    public static final class DirectoryListResult {
        public List<DirectoryEntry> entries;
    }

    public static final class GlobResult {
        public List<DirectoryEntry> matches;
    }

    public static final class GrepMatch {
        public String path;
        public int line;
        public String text;
    }

    public static final class GrepResult {
        public List<GrepMatch> matches;
        public String error;
        public boolean truncated;
    }

    public static final class ProcessStartResult {
        @JsonProperty("process_id") public String processId;
        public String status;
        @JsonProperty("started_at") public String startedAt;
    }

    public static final class ProcessInfo {
        @JsonProperty("process_id") public String processId;
        public String command;
        public String description;
        public String status;
        @JsonProperty("started_at") public String startedAt;
        @JsonProperty("exit_code") public Integer exitCode;
        @JsonProperty("expose_port") public Integer exposePort;
    }

    public static final class ProcessListResult {
        public List<ProcessInfo> processes;
    }

    public static final class ProcessOutputResult {
        public String status;
        @JsonProperty("stdout_chunk") public String stdoutChunk;
        @JsonProperty("next_offset") public long nextOffset;
        public boolean truncated;
        @JsonProperty("exit_code") public Integer exitCode;
    }

    public static final class ProcessInputResult {
        @JsonProperty("bytes_written") public int bytesWritten;
    }

    public static final class ProcessStopResult {
        public String status;
        @JsonProperty("exit_code") public Integer exitCode;
    }
}
