using System.IO;
using Lib;

class Program
{
    static void Main(string[] args)
    {
        var canvas = new Canvas();
        canvas.Add(new Circle(2));
        double Twice(double x) => x * 2;
        System.Console.WriteLine(Twice(canvas.Total()));
        var f = File.Open("x", FileMode.Open);
    }
}
