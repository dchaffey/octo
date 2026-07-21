const AgentAction = @import("agentAction.zig").AgentAction;

pub const UiMsg = union(enum) { agentAction: AgentAction };
