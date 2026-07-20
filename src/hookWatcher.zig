const std = @import("std");

pub var HookWatcher = struct {
    addr: std.Io.net.UnixAddress,
    server: std.Io.net.Server,

    pub fn init(io: std.Io, root_path: []const u8) !HookWatcher {
        const addr_p = try std.Io.net.UnixAddress.init(root_path);
        const server_p = try addr_p.listen(io, .{});
        return .{ .addr = addr_p, .server = server_p };
    }
};
