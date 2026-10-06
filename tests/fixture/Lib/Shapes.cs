using System;
using System.Collections.Generic;

namespace Lib
{
    public interface IShape { double Area(); }

    public class Circle : IShape
    {
        public double R;
        public Circle(double r) { R = r; }
        public double Area() => Math.PI * R * R;
    }

    public class Canvas
    {
        private readonly List<IShape> _shapes = new List<IShape>();
        public void Add(IShape s) { _shapes.Add(s); }
        public double Total()
        {
            double t = 0;
            foreach (var s in _shapes) t += s.Area();
            return t;
        }
        public double Total(double scale) => Total() * scale;
    }
}
