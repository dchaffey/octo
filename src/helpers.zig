const std = @import("std");

pub fn socket_name(allocator: std.mem.Allocator, root: []const u8) ![]const u8 {
    var hasher = std.hash.Wyhash.init(117);
    hasher.update(root);
    return std.fmt.allocPrint(allocator, "\x00octo-{x}", .{hasher.final()});
}
