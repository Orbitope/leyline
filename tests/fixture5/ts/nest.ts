export abstract class Mailer {
  abstract send(to: string): void;
}

export class SmtpMailer {
  send(to: string) {
    return to;
  }
}

@Module({ providers: [{ provide: Mailer, useClass: SmtpMailer }] })
export class AppModule {}

export class Signup {
  constructor(private readonly mailer: Mailer, private readonly client: ClientProxy) {}

  register(email: string) {
    this.mailer.send(email);
    this.client.emit("user_created", email);
  }
}

export class Listener {
  @EventPattern("user_created")
  handleUserCreated(data: string) {
    return data;
  }
}
