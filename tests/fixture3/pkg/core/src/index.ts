export * from "./graph.js";
export { makeId as newId } from "./ids.js";
import { Graph } from "./graph.js";

export default class Registry {
  private graphs: Graph[] = [];
  register(g: Graph): void {
    this.graphs.push(g);
  }
  count(): number {
    return this.graphs.length;
  }
}
