import { describe, expect, it } from "vitest";

import { setupStatusLine, updateApplyAllowed } from "../../app/static/settings.js";

describe("setupStatusLine", () => {
  const base = {
    source_dir: "/inbox",
    categories: [{ name: "other", folder: "/other" }],
    batch: { poll_interval_seconds: 30 },
    ocr: { mode: "balanced" },
  };

  it("shows review required by default", () => {
    expect(setupStatusLine(base)).toContain("review required");
    expect(setupStatusLine({ ...base, review: {} })).toContain("review required");
  });

  it("shows auto-file when approval is off", () => {
    expect(
      setupStatusLine({ ...base, review: { require_approval: false } }),
    ).toContain("auto-file");
  });
});

describe("updateApplyAllowed", () => {
  it("allows apply for a signed source install", () => {
    expect(
      updateApplyAllowed({
        update_available: true,
        signed: true,
        verifiable: true,
        installable: true,
      }),
    ).toBe(true);
  });

  it("hides apply when the release is only checksummed", () => {
    expect(
      updateApplyAllowed({
        update_available: true,
        signed: false,
        verifiable: true,
        installable: true,
      }),
    ).toBe(false);
  });

  it("hides apply when AppImage is not installable", () => {
    expect(
      updateApplyAllowed({
        update_available: true,
        signed: true,
        verifiable: true,
        installable: false,
      }),
    ).toBe(false);
  });

  it("allows apply for a signed AppImage when installable", () => {
    expect(
      updateApplyAllowed({
        update_available: true,
        signed: true,
        verifiable: true,
        installable: true,
        artifact_kind: "appimage",
      }),
    ).toBe(true);
  });
});
