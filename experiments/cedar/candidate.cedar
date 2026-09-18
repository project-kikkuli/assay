// Intended candidate. Its three permits are the same positive access cases as
// envelope.cedar, with the cross-tenant forbid made explicit once.
forbid (principal, action, resource)
when { principal.tenant != resource.tenant };

permit (principal, action == Action::"read", resource)
when {
    principal.tenant == resource.tenant &&
    resource has public && resource.public
};

permit (principal, action == Action::"read", resource)
when { principal.tenant == resource.tenant && principal == resource.owner };

permit (principal, action == Action::"read", resource)
when {
    principal.tenant == resource.tenant &&
    principal.roles.contains("reader")
};
