// Human-reviewed authorization envelope: no cross-tenant reads; same-tenant
// reads are limited to public documents, their owner, or a reader role.
forbid (principal, action, resource)
when { principal.tenant != resource.tenant };

permit (principal, action == Action::"read", resource)
when {
    principal.tenant == resource.tenant &&
    (
        (resource has public && resource.public) ||
        principal == resource.owner ||
        principal.roles.contains("reader")
    )
};
