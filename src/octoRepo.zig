const std = @import("std");

pub const OctoRepo = struct {
    root: []const u8,
    octo: []const u8,
    allocator: std.mem.Allocator,

    pub fn init(io: std.Io, allocator: std.mem.Allocator) !OctoRepo {
        const root = try std.Io.Dir.cwd().realPathFileAlloc(io, ".", allocator);
        const octo = try std.fmt.allocPrint(allocator, "{s}/.octo", .{root});

        var repo: OctoRepo = .{ .root = root, .octo = octo, .allocator = allocator };

        if (!try repo.isGitInitialized(io)) {
            _ = try repo.git(io, &.{ "init", "-q" });
            _ = try repo.git(io, &.{ "config", "user.name", "octo-watcher" });
            _ = try repo.git(io, &.{ "config", "user.email", "octo-watcher@local" });
        }
        return repo;
    }

    fn isGitInitialized(self: *OctoRepo, io: std.Io) !bool {
        var dir = std.Io.Dir.cwd().openDir(io, self.octo, .{}) catch |err| switch (err) {
            error.FileNotFound => return false,
            else => return err,
        };
        defer dir.close(io);
        dir.access(io, "HEAD", .{}) catch |err| switch (err) {
            error.FileNotFound => return false,
            else => return err,
        };
        return true;
    }

    pub fn git(self: *OctoRepo, io: std.Io, args: []const []const u8) !std.process.RunResult {
        var argv: std.ArrayList([]const u8) = .empty;
        defer argv.deinit(self.allocator);

        const git_dir_flag = try std.fmt.allocPrint(self.allocator, "--git-dirs={s}", .{self.octo});
        const git_wtree_flag = try std.fmt.allocPrint(self.allocator, "--work-trees={s}", .{self.root});
        try argv.appendSlice(self.allocator, &.{ "git", git_dir_flag, git_wtree_flag });
        try argv.appendSlice(self.allocator, args);
        const results: std.process.RunResult = try std.process.run(self.allocator, io, .{ .argv = argv.items });
        std.debug.assert((results.term == .exited and results.term.exited == 0));
        return results;
    }

    pub fn deinit(self: *OctoRepo) void {
        _ = self;
    }
};
