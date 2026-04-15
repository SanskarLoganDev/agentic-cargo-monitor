const express = require("express");
const path = require("path");
const multer = require("multer");

const admin = require("firebase-admin");

const PROJECT_ID = "project-5b45a270-c7ca-42fd-9f6";
const FIRESTORE_DATABASE_ID = "cargo-monitor";
const EXECUTE_ACTIONS_TOPIC = "execute-actions";
const FRONTEND_APPROVED_BY = "frontend-operator";

const appCredential = admin.credential.applicationDefault();

admin.initializeApp({
  credential: appCredential,
  projectId: PROJECT_ID
});

const db = admin.firestore();
db.settings({ databaseId: FIRESTORE_DATABASE_ID });

const app = express();
const upload = multer({ dest: "uploads/" });

app.set("view engine", "ejs");
app.set("views", path.join(__dirname, "views"));

app.use(express.urlencoded({ extended: true }));
app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));

async function getShipments() {
  const snapshot = await db.collection("shipments").get();
  return snapshot.docs.map((doc) => ({ id: doc.id, ...doc.data() }));
}

async function getShipmentById(medicineKey) {
  const doc = await db.collection("shipments").doc(medicineKey).get();
  if (!doc.exists) return null;
  return { id: doc.id, ...doc.data() };
}

async function getMetadataById(medicineKey) {
  const doc = await db.collection("shipments").doc(medicineKey).get();
  if (!doc.exists) return null;
  return { id: doc.id, ...doc.data() };
}

async function readCollectionFallback(names) {
  try {
    const snapshots = await Promise.all(names.map((name) => db.collection(name).get()));
    const byKey = new Map();

    snapshots.forEach((snapshot) => {
      snapshot.docs.forEach((doc) => {
        const row = { id: doc.id, ...doc.data() };
        const approvalId = (row.approval_id || row.approvalId || "").toString().trim();
        const dedupeKey = `${approvalId}:${row.id || ""}`;
        byKey.set(dedupeKey, row);
      });
    });

    return Array.from(byKey.values());
  } catch {
    return [];
  }
}

async function getPendingApprovals() {
  return readCollectionFallback(["pending-approvals", "pending approvals", "pending_approvals"]);
}

async function getApprovedActions() {
  return readCollectionFallback(["approved-actions", "approved actions", "approved_actions"]);
}

function isExactPendingStatus(status) {
  return normalizeStatus(status) === "pending";
}

async function getApprovedActionsPending() {
  const approvedActions = await getApprovedActions();
  return approvedActions.filter((item) => isExactPendingStatus(item?.status));
}

function normalizeStatus(status) {
  return (status || "").toString().trim().toLowerCase();
}

function getApprovalId(item) {
  const value = item?.approval_id || item?.approvalId || item?.id;
  if (value === null || value === undefined) return "";
  return String(value).trim();
}

function isHandledStatus(status, source) {
  const value = normalizeStatus(status);
  if (!value && source === "approved") return true;
  if (!value) return false;

  return (
    value.includes("approved") ||
    value.includes("handled") ||
    value.includes("resolved") ||
    value.includes("complete") ||
    value.includes("closed") ||
    value.includes("success") ||
    value.includes("done") ||
    value.includes("executed")
  );
}

function isPendingLikeStatus(status) {
  const value = normalizeStatus(status);
  if (!value) return true;

  if (isHandledStatus(value, "pending")) return false;

  return (
    value.includes("pending") ||
    value.includes("open") ||
    value.includes("await") ||
    value.includes("need") ||
    value.includes("action") ||
    value.includes("unresolved") ||
    value.includes("review")
  );
}

function toComparableSet(values) {
  const set = new Set();
  values.forEach((value) => {
    if (value === null || value === undefined) return;
    const normalized = String(value).trim().toLowerCase();
    if (normalized) set.add(normalized);
  });
  return set;
}

function matchesShipmentApproval(approval, shipment, medicineKey) {
  const shipmentKeys = toComparableSet([
    medicineKey,
    shipment?.id,
    shipment?.drug_id,
    shipment?.shipment_id,
    shipment?.drug_name
  ]);

  const approvalKeys = toComparableSet([
    approval?.id,
    approval?.approval_id,
    approval?.approvalId,
    approval?.medicine,
    approval?.medicineKey,
    approval?.medicine_key,
    approval?.shipment,
    approval?.shipmentId,
    approval?.shipment_id,
    approval?.drug_id,
    approval?.drugId,
    approval?.reference_id,
    approval?.referenceId,
    approval?.updates?.medicine,
    approval?.updates?.medicineKey,
    approval?.updates?.shipment,
    approval?.updates?.shipmentId,
    approval?.updates?.drug_id,
    approval?.updates?.drugId
  ]);

  for (const key of shipmentKeys) {
    if (approvalKeys.has(key)) return true;
  }
  return false;
}

function buildUnresolvedApprovals(pendingApprovals, approvedActions) {
  const pendingByApprovalId = new Map();
  const handledApprovalIds = new Set();

  pendingApprovals.forEach((item) => {
    const approvalId = getApprovalId(item);
    if (!approvalId) return;
    if (!isPendingLikeStatus(item?.status)) return;
    if (!pendingByApprovalId.has(approvalId)) {
      pendingByApprovalId.set(approvalId, item);
    }
  });

  approvedActions.forEach((item) => {
    const approvalId = getApprovalId(item);
    if (!approvalId) return;
    if (isHandledStatus(item?.status, "approved")) {
      handledApprovalIds.add(approvalId);
    }
  });

  const unresolved = [];
  pendingByApprovalId.forEach((item, approvalId) => {
    if (handledApprovalIds.has(approvalId)) return;
    unresolved.push(item);
  });

  return unresolved;
}

function valueToDisplay(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") {
    try {
      return JSON.stringify(value);
    } catch {
      return String(value);
    }
  }
  return String(value);
}

function normalizeUiSummary(uiSummary) {
  if (uiSummary === null || uiSummary === undefined) {
    return { type: "none", text: "No UI summary available" };
  }

  if (typeof uiSummary === "string") {
    const text = uiSummary.trim();
    return text ? { type: "text", text } : { type: "none", text: "No UI summary available" };
  }

  if (Array.isArray(uiSummary)) {
    const items = uiSummary
      .map((entry) => {
        if (entry === null || entry === undefined) return "";
        if (typeof entry === "string") return entry.trim();
        if (typeof entry === "object") {
          const pairs = Object.entries(entry)
            .map(([key, value]) => `${key}: ${valueToDisplay(value)}`)
            .join(" | ");
          return pairs.trim();
        }
        return String(entry).trim();
      })
      .filter(Boolean);

    return items.length ? { type: "list", items } : { type: "none", text: "No UI summary available" };
  }

  if (typeof uiSummary === "object") {
    const entries = Object.entries(uiSummary)
      .map(([key, value]) => ({ key, value: valueToDisplay(value) }))
      .filter((entry) => entry.value.trim() !== "");

    return entries.length ? { type: "entries", entries } : { type: "none", text: "No UI summary available" };
  }

  const fallback = String(uiSummary).trim();
  return fallback ? { type: "text", text: fallback } : { type: "none", text: "No UI summary available" };
}

function getCreatedAtValue(item) {
  return item?.created_at || item?.createdAt || item?.submittedAt || item?.submitted_at || null;
}

function toPendingApprovalView(item) {
  return {
    id: item.id || "unknown",
    approval_id: getApprovalId(item),
    status: item?.status || "",
    created_at: getCreatedAtValue(item),
    uiSummaryView: normalizeUiSummary(item?.ui_summary),
    raw: item
  };
}

function getActionNeededMap(shipments, unresolvedItems) {
  const map = {};
  shipments.forEach((shipment) => {
    map[shipment.id] = unresolvedItems.some((item) => matchesShipmentApproval(item, shipment, shipment.id));
  });
  return map;
}

async function getPendingApprovalsCount() {
  const approvedActionsPending = await getApprovedActionsPending();
  return approvedActionsPending.length;
}

async function getCurrentActionableApprovalForMedicine(medicineKey) {
  const [shipment, pendingApprovals, approvedActions] = await Promise.all([
    getShipmentById(medicineKey),
    getPendingApprovals(),
    getApprovedActions()
  ]);

  if (!shipment) {
    return { shipment: null, actionableApproval: null };
  }

  const unresolvedForShipment = buildUnresolvedApprovals(pendingApprovals, approvedActions)
    .filter((item) => matchesShipmentApproval(item, shipment, medicineKey));

  return {
    shipment,
    actionableApproval: unresolvedForShipment[0] || null
  };
}

async function getAccessToken() {
  const token = await appCredential.getAccessToken();

  if (typeof token === "string") {
    return token;
  }

  if (token?.access_token) {
    return token.access_token;
  }

  throw new Error("Unable to acquire Google access token for Pub/Sub publish.");
}

async function publishExecuteActionMessage(payload) {
  const accessToken = await getAccessToken();
  const publishUrl = `https://pubsub.googleapis.com/v1/projects/${PROJECT_ID}/topics/${EXECUTE_ACTIONS_TOPIC}:publish`;
  const messageData = Buffer.from(JSON.stringify(payload), "utf8").toString("base64");

  const response = await fetch(publishUrl, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${accessToken}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      messages: [{ data: messageData }]
    })
  });

  if (!response.ok) {
    let errorText = "";
    try {
      errorText = await response.text();
    } catch {
      errorText = "";
    }

    throw new Error(`Pub/Sub publish failed (${response.status}): ${errorText || "empty body"}`);
  }

  return response.json();
}

// TEMP TEST ROUTE - delete after confirming Firestore works
app.get("/test-firestore", async (req, res) => {
  try {
    const snapshot = await db.collection("shipments").get();
    const docs = snapshot.docs.map((doc) => ({ id: doc.id, ...doc.data() }));
    res.json({
      status: "Firestore connected!",
      count: docs.length,
      documents: docs
    });
  } catch (err) {
    res.json({ status: "Firestore FAILED", error: err.message });
  }
});

app.get("/", async (req, res) => {
  try {
    const [shipments, pendingApprovals, approvedActions, pendingApprovalsCount] = await Promise.all([
      getShipments(),
      getPendingApprovals(),
      getApprovedActions(),
      getPendingApprovalsCount()
    ]);

    const unresolvedApprovals = buildUnresolvedApprovals(pendingApprovals, approvedActions);
    const actionNeededByMedicine = getActionNeededMap(shipments, unresolvedApprovals);

    res.render("dashboard", {
      medicines: shipments,
      activeShipmentsCount: shipments.length,
      pendingApprovalsCount,
      pendingApprovalsLabel: null,
      actionNeededByMedicine,
      showUpload: true,
      sidebarCollapsed: false
    });
  } catch (err) {
    console.error("Dashboard error:", err);
    res.status(500).send("Error loading dashboard");
  }
});

app.get("/shipment/:medicineKey", async (req, res) => {
  try {
    const medicineKey = req.params.medicineKey;
    const [shipment, approvals, approvedActions] = await Promise.all([
      getShipmentById(medicineKey),
      getPendingApprovals(),
      getApprovedActions()
    ]);

    if (!shipment) return res.status(404).send("Shipment not found");

    const pendingForShipment = buildUnresolvedApprovals(approvals, approvedActions)
      .filter((item) => matchesShipmentApproval(item, shipment, medicineKey));

    const pendingApprovalItems = pendingForShipment.map(toPendingApprovalView);
    const hasRisk = pendingApprovalItems.length > 0;

    res.render("shipment-detail", {
      shipment,
      medicineKey,
      hasRisk,
      hasPendingApproval: hasRisk,
      pendingApprovalItems,
      showUpload: false,
      sidebarCollapsed: true
    });
  } catch (err) {
    console.error("Shipment detail error:", err);
    res.status(500).send("Error loading shipment");
  }
});

app.get("/metadata/:medicineKey", async (req, res) => {
  try {
    const medicineKey = req.params.medicineKey;
    const metadata = await getMetadataById(medicineKey);

    if (!metadata) return res.status(404).send("Metadata not found");

    res.render("metadata-detail", {
      metadata,
      medicineKey,
      showUpload: false,
      sidebarCollapsed: true
    });
  } catch (err) {
    console.error("Metadata error:", err);
    res.status(500).send("Error loading metadata");
  }
});

app.get("/api/pending-approvals/count", async (req, res) => {
  try {
    const pendingApprovedActions = await getApprovedActionsPending();
    res.json({ count: pendingApprovedActions.length, items: pendingApprovedActions });
  } catch {
    res.json({ count: 0, items: [] });
  }
});

async function handleApproveAction(req, res) {
  try {
    const { medicineKey } = req.params;
    const { shipment, actionableApproval } = await getCurrentActionableApprovalForMedicine(medicineKey);

    if (!shipment) {
      return res.status(404).json({ status: "not_found", message: "Shipment not found." });
    }

    if (!actionableApproval) {
      return res.status(404).json({
        status: "no_action_needed",
        message: "No pending approvals requiring action were found for this shipment."
      });
    }

    const approvalId = getApprovalId(actionableApproval);
    if (!approvalId) {
      return res.status(400).json({
        status: "invalid_approval",
        message: "Matching approval is missing approval_id."
      });
    }

    if (!isPendingLikeStatus(actionableApproval.status)) {
      return res.status(409).json({
        status: "already_handled",
        message: "Approval is not in an actionable pending state."
      });
    }

    const fullPendingApprovalDoc = actionableApproval;
    const approvedAt = new Date().toISOString();
    const publishPayload = {
      ...fullPendingApprovalDoc,
      document_id: fullPendingApprovalDoc.id || approvalId,
      approval_id: fullPendingApprovalDoc.approval_id || approvalId,
      approved_at: approvedAt,
      approved_by: FRONTEND_APPROVED_BY
    };

    try {
      await publishExecuteActionMessage(publishPayload);
      await Promise.all([
        db.collection("pending-approvals").doc(approvalId).set({
          status: "approved",
          approved_at: approvedAt,
          approved_by: FRONTEND_APPROVED_BY
        }, { merge: true }),
        db.collection("approved-actions").doc(approvalId).set({
          ...publishPayload,
          status: "approved"
        }, { merge: true })
      ]);
    } catch (err) {
      console.error("Pub/Sub execute publish failed:", err);
      return res.status(502).json({ ok: false, error: "pubsub_failed" });
    }

    return res.json({
      ok: true,
      approval_id: publishPayload.approval_id || approvalId
    });
  } catch (err) {
    console.error("Approve action error:", err);
    return res.status(500).json({ ok: false, error: "pubsub_failed" });
  }
}

app.post("/api/shipment/:medicineKey/approve-action", handleApproveAction);

app.post("/api/approval/:medicineKey/approve", async (req, res) => {
  return handleApproveAction(req, res);
});

app.post("/api/shipment/:medicineKey/save-updates", async (req, res) => {
  try {
    const { medicineKey } = req.params;
    const updates = req.body?.updates || {};

    if (!Object.keys(updates).length) {
      return res.status(400).json({ status: "no_changes", message: "No updates to save." });
    }

    return res.json({
      status: "queued",
      medicineKey,
      updates,
      message: "Updates captured - placeholder for Service B handoff."
    });
  } catch (err) {
    console.error("Save updates error:", err);
    res.status(500).json({ status: "error", message: "Failed to save updates." });
  }
});

app.post("/upload", upload.single("file"), (req, res) => {
  res.json({
    status: "received",
    filename: req.file?.originalname || "unknown",
    message: "PDF uploaded successfully."
  });
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`AgenticTerps running at http://localhost:${PORT}`));
