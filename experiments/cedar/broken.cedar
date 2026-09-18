// Deliberate regression: the broad permit has no tenant or visibility guard.
permit (principal, action == Action::"read", resource);
