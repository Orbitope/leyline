namespace Mod;

public class Level
{
    public int Size;
    public int Step() => Size;
}

public class Sim
{
    public Sim(Level level, int seed) { }
    public int Step() => 1;
}

public static class Made
{
    public static Sim Build() => new Sim(new Level { Size = 3 }, 1);

    public static int Check(object o)
    {
        if (o is Sim s)
            return s.Step();
        return 0;
    }

    static int Rate(int o, float perMinute) => 1;
    static int Rate(int o, Level level) => 2;

    public static int Mix(float total, float share) => Rate(1, total * share) + Rate(2, 3f);
}
