using System;

namespace Mod;

public class Limit { public Limit(int n) { } }

public static class Runner
{
    public static void Run(Action<int> one) { }
    public static void Run(Action<int, int> two) { }
    public static int Make(int seed) => Make<int>(seed);
    public static int Make<T>(int seed) => seed;
    public static void Wait(int seconds) { }
    public static void Wait(Limit limit) { }

    public static void Main()
    {
        Run(a => { });
        Run((a, b) => { });
        Wait(new Limit(3));
        var plain = new Mod.Boxes.Box();
        var generic = new Mod.Boxes.Box<int>();
    }
}
