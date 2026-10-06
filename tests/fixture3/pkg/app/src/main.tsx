import { memo, useCallback } from "react";
import Registry, { build, newId, Graph } from "@fx/core";
import { store } from "@fx/core/ids";
import * as core from "@fx/core";
import { readFileSync } from "node:fs";

interface RowProps {
  label: string;
  onPick: () => void;
}

const Row = memo((props: RowProps) => <button onClick={props.onPick}>{props.label}</button>);

function handlePick() {
  const g = build();
  g.touch();
  store.save("k", newId("v"));
}

export function Panel({ graph }: { graph: Graph }) {
  const refresh = useCallback(() => {
    core.build().size();
  }, []);
  return (
    <div>
      <Row label="a" onPick={handlePick} />
      <Row label="b" onPick={refresh} />
    </div>
  );
}

export async function main() {
  const registry = new Registry();
  registry.register(build());
  const res = await fetch(`/api/graphs/${registry.count()}`, { method: "POST" });
  readFileSync("data/graphs.json");
  return res;
}

main();
