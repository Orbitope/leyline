using System.Collections.Generic;

namespace Mod.Shapes;

public interface IPricer { int Price(int qty); }
public class FlatPricer : IPricer { public int Price(int qty) => qty; }
public class BulkPricer : IPricer { public int Price(int qty) => qty / 2; }

// Decorator: an IPricer that wraps another.
public class TaxedPricer : IPricer
{
    private readonly IPricer _inner;
    public TaxedPricer(IPricer inner) { _inner = inner; }
    public int Price(int qty) => _inner.Price(qty) + 1;
}

// Composite: an IPricer made of many.
public class SumPricer : IPricer
{
    private readonly List<IPricer> _parts = new List<IPricer>();
    public int Price(int qty) { int t = 0; foreach (var p in _parts) t += p.Price(qty); return t; }
}

// Strategy context: holds one and calls it.
public class Checkout
{
    private readonly IPricer _pricer;
    public Checkout(IPricer pricer) { _pricer = pricer; }
    public int Total(int qty) => _pricer.Price(qty);
}

// Factory: chooses the implementation and returns the abstraction.
public static class Pricers
{
    public static IPricer For(bool bulk)
    {
        if (bulk) return new BulkPricer();
        return new FlatPricer();
    }
}

// Template method: Run is fixed, Step is supplied by subclasses.
public abstract class Job
{
    public void Run() { Step(); }
    protected abstract void Step();
}
public class PrintJob : Job { protected override void Step() { } }

// Singleton.
public sealed class Clock
{
    public static readonly Clock Instance = new Clock();
    private Clock() { }
}

// Builder.
public class Order { }
public class OrderBuilder
{
    public OrderBuilder With(int qty) => this;
    public OrderBuilder Named(string name) => this;
    public Order Build() => new Order();
}
