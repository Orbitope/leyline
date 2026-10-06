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
