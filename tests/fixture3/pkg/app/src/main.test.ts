import { describe, it, expect } from "vitest";
import { build } from "@fx/core";

describe("graph", () => {
  it("adds a node", () => {
    const g = build();
    g.add({ id: "x", label: "y" });
    expect(g.size()).toBe(2);
  });

  it.each([1, 2])("counts %d", (n) => {
    expect(build().size()).toBe(n);
  });
});
