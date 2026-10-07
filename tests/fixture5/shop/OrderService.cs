namespace Shop;

public interface INotifier
{
    void Notify(string text);
}

public class SmsNotifier : INotifier
{
    public void Notify(string text) { }
}

public record OrderPlaced(string Item);

public class OrderPlacedHandler : INotificationHandler<OrderPlaced>
{
    public Task Handle(OrderPlaced message, CancellationToken token) => Task.CompletedTask;
}

public class OrderService
{
    private readonly IOrderStore _store;
    private readonly IMediator _mediator;
    private readonly IModel _channel;

    public OrderService(IOrderStore store, IMediator mediator, IModel channel)
    {
        _store = store;
        _mediator = mediator;
        _channel = channel;
    }

    public void Place(string item)
    {
        _store.Save(new Order { Item = item });
        _mediator.Publish(new OrderPlaced(item));
        _channel.BasicPublish(exchange: "", routingKey: "orders.placed", body: null);
    }
}

public class OutboxWorker : BackgroundService
{
    protected override Task ExecuteAsync(CancellationToken token) => Task.CompletedTask;
}

public class GreeterService : Greeter.GreeterBase
{
    public override Task<HelloReply> SayHello(HelloRequest request, ServerCallContext context) => null;
}
