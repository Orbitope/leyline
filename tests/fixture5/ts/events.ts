import { EventEmitter } from "events";

export const bus = new EventEmitter();

export function onShipped(id: string) {
  return id;
}

export function wire() {
  bus.on("order.shipped", onShipped);
  process.on("exit", onShipped);
}

export function ship(id: string) {
  bus.emit("order.shipped", id);
  bus.emit("error", id);
}
