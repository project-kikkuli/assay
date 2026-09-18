import type { ItemUpdate } from "./src/client/types.gen";

// Compile, never execute. A rejection is an observation, not a broken harness.
const update: ItemUpdate = { title: null };
void update;
