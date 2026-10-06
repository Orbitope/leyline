import { makeId } from "./ids.js";

export interface Item {
  id: string;
  label: string;
}

type Stamped = { at: number };
export type Link = Stamped & { from: string; to: string };

export enum Mode { Fast, Safe }

export class Graph {
  private items: Item[] = [];
  mode: Mode = Mode.Fast;

  add(item: Item): void {
    this.items.push(item);
    this.touch();
  }

  touch(): void {}

  size(): number {
    return this.items.length;
  }

  rename(item: Item, label: string): void {
    item.label = label;
  }
}

export function build(): Graph {
  const g = new Graph();
  g.add({ id: makeId("n"), label: "first" });
  return g;
}

export function describeLink(link: Link): string {
  return link.from + "->" + link.to + "@" + link.at;
}
