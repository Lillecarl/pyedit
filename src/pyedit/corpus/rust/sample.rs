fn alpha(x: u32) -> u32 {
    x
}

struct Beta {
    gamma: u32,
}

impl Beta {
    fn gamma(&self) -> u32 {
        self.gamma
    }
}
