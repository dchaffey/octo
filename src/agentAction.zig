const std = @import("std");
const OctoArgs = @import("octoArgs.zig").OctoArgs;

// Just the string of the tool that was called.
const ToolCall = []const u8;

pub const AgentAction = struct {
    timestamp: i64,
    agent: OctoArgs.First,
    session_id: []const u8,
    prompt_id: []const u8,

    fileEdits: []const FileEdit,
    toolCalls: []const ToolCall,
};

pub const FileEdit = struct {
    editContent: EditContent,
    file_path: []const u8,
};

pub const EditContent = union(enum) {
    file_diff: []const FileDiff,
    file_creation: []const u8,
    file_overwrite: []const u8,
    file_deletion: bool,
};

pub const FileDiff = struct {
    old_fragment: []const u8,
    new_fragment: []const u8,
};
