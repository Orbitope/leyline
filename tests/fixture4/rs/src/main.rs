mod geo;
use geo::Circle;

fn main() {
    let c = Circle::new(2.0);
    let a = c.area();
    let s = format!("{}", a);
    shout!(s);
    let n = s.len();
}

#[test]
fn area_is_positive() {
    assert!(Circle::new(1.0).area() > 0.0);
}
