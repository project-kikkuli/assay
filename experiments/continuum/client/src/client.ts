type Reservation = {
  id: string;
  tenant: string;
  creator: string;
  quantity: number;
  status: string;
};

type Identity = {
  token: string;
  tenant: string;
  role: "maker" | "reviewer";
};

type ApiResponse = {
  items?: Reservation[];
  next_cursor?: string | null;
  id?: string;
  tenant?: string;
  creator?: string;
  quantity?: number;
  status?: string;
  error?: string;
};

const identities: Identity[] = [
  { token: "alpha-maker", tenant: "alpha", role: "maker" },
  { token: "alpha-reviewer", tenant: "alpha", role: "reviewer" },
  { token: "beta-maker", tenant: "beta", role: "maker" },
  { token: "beta-reviewer", tenant: "beta", role: "reviewer" },
];

function requiredElement<T extends Element>(selector: string): T {
  const element = document.querySelector<T>(selector);
  if (!element) throw new Error(`continuum UI markup is missing ${selector}`);
  return element;
}

const identitySelect = requiredElement<HTMLSelectElement>("#identity");
const quantityInput = requiredElement<HTMLInputElement>("#quantity");
const keyInput = requiredElement<HTMLInputElement>("#command-key");
const reserveButton = requiredElement<HTMLButtonElement>("#reserve");
const listButton = requiredElement<HTMLButtonElement>("#refresh");
const statusOutput = requiredElement<HTMLOutputElement>("#status");
const reservationsList = requiredElement<HTMLTableSectionElement>("#reservations-body");
const identityDescription = requiredElement<HTMLElement>("#identity-description");

const apiBase = (window as Window & { __ASSAY_API__?: string }).__ASSAY_API__ ?? window.location.origin;

function currentIdentity(): Identity {
  return identities.find((identity) => identity.token === identitySelect.value) ?? identities[0]!;
}

function newKey(prefix: string): string {
  const random = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  return `${prefix}-${random}`;
}

function setStatus(message: string, isError = false): void {
  statusOutput.textContent = message;
  statusOutput.dataset.state = isError ? "error" : "ok";
}

function updateIdentityDescription(): void {
  const identity = currentIdentity();
  identityDescription.textContent = `${identity.tenant} tenant · ${identity.role} role`;
}

async function apiRequest(path: string, body?: unknown): Promise<ApiResponse> {
  const identity = currentIdentity();
  const response = await fetch(new URL(path.replace(/^\//, ""), `${apiBase.replace(/\/$/, "")}/`), {
    method: body === undefined ? "GET" : "POST",
    headers: {
      authorization: `Bearer ${identity.token}`,
      ...(body === undefined ? {} : { "content-type": "application/json" }),
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const payload: unknown = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = typeof payload === "object" && payload !== null && "error" in payload && typeof payload.error === "string"
      ? payload.error
      : `request failed (${response.status})`;
    throw new Error(error.slice(0, 240));
  }
  return payload as ApiResponse;
}

function appendCell(row: HTMLTableRowElement, text: string): void {
  const cell = document.createElement("td");
  cell.textContent = text;
  row.append(cell);
}

function actionButton(label: string, onClick: () => Promise<void>): HTMLButtonElement {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.setAttribute("aria-label", label);
  button.addEventListener("click", () => void onClick());
  return button;
}

function renderReservations(items: Reservation[]): void {
  reservationsList.replaceChildren();
  if (items.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "No reservations";
    row.append(cell);
    reservationsList.append(row);
    return;
  }
  for (const reservation of items) {
    const row = document.createElement("tr");
    appendCell(row, reservation.id);
    appendCell(row, reservation.creator);
    appendCell(row, String(reservation.quantity));
    appendCell(row, reservation.status);
    const actions = document.createElement("td");
    actions.className = "actions";
    const identity = currentIdentity();
    if (identity.role === "reviewer" && reservation.status === "reserved") {
      actions.append(actionButton(`Approve ${reservation.id}`, async () => {
        await mutate("approve", reservation.id);
      }));
    }
    if (identity.role === "maker" && identity.token === reservation.creator && reservation.status === "reserved") {
      actions.append(actionButton(`Cancel ${reservation.id}`, async () => {
        await mutate("cancel", reservation.id);
      }));
    }
    if (actions.childElementCount === 0) actions.textContent = "—";
    row.append(actions);
    reservationsList.append(row);
  }
}

async function listReservations(): Promise<void> {
  try {
    setStatus("Loading reservations…");
    const payload = await apiRequest("/reservations?limit=100");
    renderReservations(payload.items ?? []);
    setStatus(`${payload.items?.length ?? 0} reservation(s) loaded.`);
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "Unable to load reservations.", true);
  }
}

async function mutate(op: "approve" | "cancel", reservationId: string): Promise<void> {
  try {
    setStatus(`${op === "approve" ? "Approving" : "Cancelling"} ${reservationId}…`);
    await apiRequest("/commands", { op, key: newKey(op), reservation_id: reservationId });
    setStatus(`${op === "approve" ? "Approved" : "Cancelled"} ${reservationId}.`);
    await listReservations();
  } catch (error) {
    setStatus(error instanceof Error ? error.message : `Unable to ${op} reservation.`, true);
  }
}

reserveButton.addEventListener("click", async () => {
  const quantity = Number(quantityInput.value);
  const key = keyInput.value.trim();
  if (!Number.isInteger(quantity) || quantity < 1 || quantity > 8) {
    setStatus("Quantity must be an integer from 1 to 8.", true);
    quantityInput.focus();
    return;
  }
  if (!key) {
    setStatus("Enter an idempotency key.", true);
    keyInput.focus();
    return;
  }
  try {
    setStatus("Reserving…");
    await apiRequest("/commands", { op: "reserve", key, quantity });
    keyInput.value = newKey("reserve");
    setStatus("Reservation created.");
    await listReservations();
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "Unable to reserve.", true);
  }
});

listButton.addEventListener("click", () => void listReservations());
identitySelect.addEventListener("change", () => {
  updateIdentityDescription();
  void listReservations();
});

for (const identity of identities) {
  const option = document.createElement("option");
  option.value = identity.token;
  option.textContent = `${identity.token} (${identity.tenant} ${identity.role})`;
  identitySelect.append(option);
}
keyInput.value = newKey("reserve");
updateIdentityDescription();
void listReservations();
