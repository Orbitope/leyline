namespace Mod;

public static class Hard
{
    static int Pick(int x) => 1;
    static int Pick(double x) => 2;
    static double Half(int v) => v / 2.0;

    public static int Go()
    {
        var h = Half(3);
        return Pick(h);
    }
}
