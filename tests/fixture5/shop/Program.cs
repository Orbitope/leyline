using Microsoft.Extensions.DependencyInjection;

namespace Shop;

public static class Program
{
    public static void Main(string[] args)
    {
        var services = new ServiceCollection();
        Configure(services);
        var orders = new OrderService(null, null, null);
        orders.Place("book");
    }

    public static void Configure(IServiceCollection services)
    {
        services.AddScoped<IOrderStore, SqlOrderStore>();
        services.AddSingleton<INotifier>(sp => new SmsNotifier());
        services.AddSingleton<Reports>();
        services.AddHostedService<OutboxWorker>();
        services.AddSingleton<IClock>(sp => sp.GetRequiredService<SystemClock>());
        services.AddScoped<IPrinter, Printer>();
        services.AddScoped<IPrinter, Printer>();
    }
}
