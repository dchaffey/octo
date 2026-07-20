const std = @import("std");

const Agent = enum { Claude, Agy, Codex };

// Just the string of the tool that was called.
const ToolCall = []const u8;

pub const AgentAction = struct {
    timestamp: i64,
    agent: Agent,
    session_id: []const u8,
    prompt_text: []const u8,

    fileEdits: []const FileEdit,
    toolCalls: []const ToolCall,
};

pub const FileEdit = struct {
    editContent: EditContent,
    file_path: []const u8,
};

pub const EditContent = union(enum) {
    replacements: []const Replacement,
    full_content: []const u8,
};

pub const Replacement = struct {
    old_fragment: []const u8,
    new_fragment: []const u8,
};
