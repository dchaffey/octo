const std = @import("std");
const agentAction = @import("../agentAction.zig");
const OctoArgs = @import("../octoArgs.zig").OctoArgs;

// Fixed top-level shape of the payload Claude Code writes to stdin for a
// PostToolUse hook invocation, trimmed to only the fields octo uses.
// tool_input/tool_response vary by tool_name (Write/Edit/Bash/Read each have
// a different shape), so they're left as raw JSON values and dispatched on
// tool_name by the caller. tool_input is needed alongside tool_response
// because some tools (Bash) don't echo their input back in the response --
// the command text only exists in tool_input. Unused fields (permission_mode,
// effort, duration_ms, transcript_path, tool_use_id) are omitted; parsing
// with .ignore_unknown_fields = true skips them in the source JSON.
pub const ClaudePostToolUse = struct {
    session_id: []const u8, // Claude session id — correlates hook events within one conversation
    cwd: []const u8, // working directory the hook fired from
    prompt_id: []const u8, // id of the user prompt that triggered this tool call
    hook_event_name: []const u8, // always "PostToolUse" for this payload shape
    tool_name: []const u8, // which tool ran ("Write", "Edit", "Bash", "Read", ...); selects tool_input/tool_response parsing
    tool_input: std.json.Value, // tool-specific input args; shape depends on tool_name
    tool_response: std.json.Value, // tool-specific result; shape depends on tool_name

    // Builds an AgentAction from an Edit-tool PostToolUse payload. Only valid when
    // tool_name == "Edit"; caller is responsible for checking that first. Copies every
    // string into `allocator` so the result stays valid after the caller's json.Parsed
    // (backing self's fields) is deinit'd.
    pub fn toEditAgentAction(self: ClaudePostToolUse, allocator: std.mem.Allocator, timestamp: i64) !agentAction.AgentAction {
        const edit_response = try std.json.parseFromValue(EditResponse, allocator, self.tool_response, .{ .ignore_unknown_fields = true });
        defer edit_response.deinit();

        const file_diff: agentAction.FileDiff = .{
            .old_fragment = try allocator.dupe(u8, edit_response.value.oldString),
            .new_fragment = try allocator.dupe(u8, edit_response.value.newString),
        };
        const edit_content: agentAction.EditContent = .{ .file_diff = try allocator.dupe(agentAction.FileDiff, &.{file_diff}) };
        const file_edit: agentAction.FileEdit = .{
            .editContent = edit_content,
            .file_path = try allocator.dupe(u8, edit_response.value.filePath),
        };

        return .{
            .timestamp = timestamp,
            .agent = OctoArgs.First.claude,
            .session_id = try allocator.dupe(u8, self.session_id),
            .prompt_id = try allocator.dupe(u8, self.prompt_id),
            .fileEdits = try allocator.dupe(agentAction.FileEdit, &.{file_edit}),
            .toolCalls = &.{},
        };
    }

    // Builds an AgentAction from a Bash-tool PostToolUse payload. Only valid when
    // tool_name == "Bash"; caller is responsible for checking that first. The command
    // text lives in tool_input (tool_response only carries stdout/stderr), so this reads
    // tool_input instead of tool_response, unlike toEditAgentAction.
    pub fn toBashAgentAction(self: ClaudePostToolUse, allocator: std.mem.Allocator, timestamp: i64) !agentAction.AgentAction {
        const bash_input = try std.json.parseFromValue(BashInput, allocator, self.tool_input, .{ .ignore_unknown_fields = true });
        defer bash_input.deinit();

        return .{
            .timestamp = timestamp,
            .agent = OctoArgs.First.claude,
            .session_id = try allocator.dupe(u8, self.session_id),
            .prompt_id = try allocator.dupe(u8, self.prompt_id),
            .fileEdits = &.{},
            .toolCalls = try allocator.dupe([]const u8, &.{try allocator.dupe(u8, bash_input.value.command)}),
        };
    }
};

// tool_input shape when tool_name == "Bash".
pub const BashInput = struct {
    command: []const u8, // shell command text that was executed
    description: ?[]const u8 = null, // agent-provided one-line description of the command's purpose
};

// tool_response shape when tool_name == "Write".
pub const WriteResponse = struct {
    type: []const u8, // "create" or "update", how the write affected the file
    filePath: []const u8, // absolute path of the file that was written
    content: []const u8, // full file content after the write
    structuredPatch: []const std.json.Value, // diff hunks, empty on a fresh create
    originalFile: ?[]const u8 = null, // prior file content, null when newly created
    userModified: bool, // whether the user hand-edited the content before it was applied
};

// tool_response shape when tool_name == "Edit".
pub const EditResponse = struct {
    filePath: []const u8, // absolute path of the file that was edited
    oldString: []const u8, // fragment that was replaced
    newString: []const u8, // fragment it was replaced with
    originalFile: ?[]const u8 = null, // file content prior to the edit
    structuredPatch: []const std.json.Value, // diff hunks describing the edit
    userModified: bool, // whether the user hand-edited the replacement before it was applied
    replaceAll: bool, // whether every occurrence of oldString was replaced
};
