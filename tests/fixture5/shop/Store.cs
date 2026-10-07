using System.Linq;

namespace Shop;

public interface IOrderStore
{
    void Save(Order order);
}

public class SqlOrderStore : IOrderStore
{
    private readonly ShopDb _db;

    public SqlOrderStore(ShopDb db) { _db = db; }

    public void Save(Order order)
    {
        _db.Orders.Add(order);
        _db.SaveChanges();
    }
}

public class MemoryOrderStore : IOrderStore
{
    public void Save(Order order) { }
}

public class Order
{
    public string Item { get; set; }
}

public class ShopDb : DbContext
{
    public DbSet<Order> Orders { get; set; }
}

public class Reports
{
    private readonly ShopDb _context;

    public Reports(ShopDb context) { _context = context; }

    public int Count() => _context.Orders.Count();
}
