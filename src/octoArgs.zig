pub const OctoArgs = struct {
    pub const First = enum { claude, uninstall };

    pub const ClaudeHook = enum { claudeStop, claudePostToolUse };
};
