using System;
using Lib;

static class T
{
    public static void Run(string name, Action test) { test(); }
}

class Checks
{
    int _seen;
    void OnChanged(int v) { _seen = v; }

    void All()
    {
        T.Run("bus notifies a subscriber", () =>
        {
            var bus = new Bus();
            bus.Changed += OnChanged;
            bus.Set(3);
        });
        T.Run("canvas totals", () =>
        {
            var c = new Canvas();
            c.Total();
        });
    }
}
