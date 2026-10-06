import { build } from "@fx/core";

export function routes(app: any) {
  app.post("/api/graphs/:id", async () => {
    return build().size();
  });
}
