pub struct Circle {
    pub r: f64,
}

impl Circle {
    pub fn new(r: f64) -> Self {
        Circle { r }
    }
    pub fn area(&self) -> f64 {
        3.14 * self.r * self.r
    }
}

macro_rules! shout {
    ($x:expr) => { println!("{}!", $x) };
}

pub fn format(c: &Circle) -> String {
    String::from("circle")
}
