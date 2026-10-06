let counter = 0;

export function makeId(prefix: string): string {
  counter += 1;
  return prefix + counter;
}

export const store = {
  load(key: string) {
    return makeId(key);
  },
  save: (key: string, value: string) => {
    return key + value;
  },
};
