namespace Mod.State;

public class Counter
{
    public int Value;
    public int Limit;
    private int _hits;
    public int[] Slots = new int[4];

    public void Bump() { Value++; _hits = _hits + 1; }
    public bool Full() => Value >= Limit;
}

public static class Driver
{
    public static Counter Make()
    {
        var c = new Counter { Limit = 3 };
        c.Value = 1;
        return c;
    }

    public static int Peek(Counter c)
    {
        int Value = 5;
        return c.Limit + Value;
    }

    public static void Reset(Counter counter)
    {
        counter.Value = 0;
        Make().Slots[0] = 2;
    }
}

public class Journal
{
    private readonly System.Collections.Generic.List<int> _items = new System.Collections.Generic.List<int>();
    private readonly System.Collections.Generic.Dictionary<int, System.Collections.Generic.Queue<int>> _by
        = new System.Collections.Generic.Dictionary<int, System.Collections.Generic.Queue<int>>();

    public void Note(int x) { _items.Add(x); _by[x].Enqueue(x); }
    public int Count() => _items.Count + _by.Count;
}
