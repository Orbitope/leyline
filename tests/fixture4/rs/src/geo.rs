pub struct Circle {
    pub r: f64,
}

pub trait Shape {
    fn area(&self) -> f64;
}

impl Circle {
    pub fn new(r: f64) -> Self {
        Circle { r }
    }
    pub fn area(&self) -> f64 {
        3.14 * self.r * self.r
    }
    pub fn describe(&self) -> String {
        String::from("a circle")
    }
}

impl Shape for Circle {
    fn area(&self) -> f64 {
        Circle::area(self)
    }
}

macro_rules! shout {
    ($x:expr) => { println!("{}!", $x) };
}

pub fn format(c: &Circle) -> String {
    String::from("circle")
}

pub fn describe(c: &Circle) -> String {
    c.describe()
}
