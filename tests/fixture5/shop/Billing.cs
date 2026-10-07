using System.Net.Http;

namespace Shop;

public interface IClock
{
    long Now();
}

public class SystemClock : IClock
{
    public long Now() => 0;
}

public interface IPrinter
{
    void Print(string text);
    void Print(int copies);
}

public class Printer : IPrinter
{
    public void Print(int copies) { }
    public void Print(string text) { }
}

public record OrderShipped(string Item);

public record ReceiptRequestedEvent(string Item);

public class ShippedHandler : INotificationHandler<OrderShipped>
{
    public Task Handle(OrderShipped e, CancellationToken token) => Task.CompletedTask;
}

public class ReceiptRequestedEventHandler : IIntegrationEventHandler<ReceiptRequestedEvent>
{
    public Task Handle(ReceiptRequestedEvent e) => Task.CompletedTask;
}

public class Shipment : Entity
{
    public void Ship(string item)
    {
        AddDomainEvent(new OrderShipped(item));
    }
}

public class Outbox
{
    public void Add(object message) { }
}

public class Billing
{
    private readonly Outbox _outbox;
    private readonly HttpClient _http;
    private readonly IRepository<Order> _orders;

    public void Bill(string item)
    {
        var receipt = new ReceiptRequestedEvent(item);
        _outbox.Add(receipt);
        _http.SendAsync(new HttpRequestMessage());
    }

    public int Pending() => _orders.ListAsync().Result.Count;

    public void Close(Order order) => _orders.DeleteAsync(order);
}

public class GreeterCaller
{
    private Greeter.GreeterClient _client;

    private Greeter.GreeterClient Client() => _client;

    public void Call() => Client().SayHelloAsync(new HelloRequest());
}
