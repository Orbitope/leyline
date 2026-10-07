export class RecipesResolver {
  constructor(private readonly pubSub: PubSub) {}

  addRecipe(name: string) {
    this.pubSub.publish("recipeAdded", { recipeAdded: name });
  }

  recipeAdded() {
    return this.pubSub.asyncIterator("recipeAdded");
  }
}
