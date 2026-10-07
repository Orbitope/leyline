export async function addUser(prisma: any, email: string) {
  return prisma.user.create({ data: { email } });
}

export async function listUsers(knex: any) {
  return knex("users").where({ active: true }).select("email");
}
