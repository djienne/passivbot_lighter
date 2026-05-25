use std::collections::VecDeque;

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum HardStopTier {
    Green,
    Yellow,
    Orange,
    Red,
}

impl Default for HardStopTier {
    fn default() -> Self {
        Self::Green
    }
}

#[derive(Debug, Clone, Copy)]
pub struct HardStopTierRatios {
    pub yellow: f64,
    pub orange: f64,
}

impl Default for HardStopTierRatios {
    fn default() -> Self {
        Self {
            yellow: 0.5,
            orange: 0.75,
        }
    }
}

impl HardStopTierRatios {
    pub fn validate(self) -> Result<(), String> {
        if !(self.yellow.is_finite() && self.orange.is_finite()) {
            return Err("tier ratios must be finite".to_string());
        }
        if !(0.0 < self.yellow && self.yellow < self.orange && self.orange < 1.0) {
            return Err("tier ratios must satisfy 0 < yellow < orange < 1".to_string());
        }
        Ok(())
    }
}

#[derive(Debug, Clone, Copy)]
pub struct HardStopConfig {
    pub red_threshold: f64,
    pub ema_span_minutes: f64,
    pub tier_ratios: HardStopTierRatios,
}

impl HardStopConfig {
    pub fn validate(self) -> Result<(), String> {
        if !self.red_threshold.is_finite() || self.red_threshold <= 0.0 {
            return Err("red_threshold must be finite and > 0".to_string());
        }
        if !self.ema_span_minutes.is_finite() || self.ema_span_minutes <= 0.0 {
            return Err("ema_span_minutes must be finite and > 0".to_string());
        }
        self.tier_ratios.validate()
    }
}

#[derive(Debug, Clone, Copy, Default)]
pub struct HardStopState {
    pub peak_strategy_equity: f64,
    pub drawdown_ema: f64,
    pub tier: HardStopTier,
    pub red_latched: bool,
    pub initialized: bool,
    pub last_minute: Option<u64>,
    pub cached_step: Option<HardStopStep>,
}

#[derive(Debug, Clone, Copy)]
#[allow(dead_code)]
pub struct HardStopStep {
    pub drawdown_raw: f64,
    pub drawdown_score: f64,
    pub tier: HardStopTier,
    pub changed: bool,
    pub alpha: f64,
    pub elapsed_minutes: u64,
}

#[derive(Debug, Clone, Default)]
#[allow(dead_code)]
pub struct RollingPeakTracker {
    peaks: VecDeque<(u64, f64)>,
    last_timestamp_ms: Option<u64>,
}

#[allow(dead_code)]
impl RollingPeakTracker {
    pub fn reset(&mut self) {
        self.peaks.clear();
        self.last_timestamp_ms = None;
    }

    pub fn len(&self) -> usize {
        self.peaks.len()
    }

    pub fn update(
        &mut self,
        timestamp_ms: u64,
        equity: f64,
        lookback_ms: u64,
    ) -> Result<f64, String> {
        if !equity.is_finite() {
            return Err("equity must be finite".to_string());
        }
        if lookback_ms == 0 {
            return Err("lookback_ms must be > 0".to_string());
        }
        if let Some(prev_ts) = self.last_timestamp_ms {
            if timestamp_ms < prev_ts {
                return Err(format!(
                    "timestamp_ms must be non-decreasing, got {} after {}",
                    timestamp_ms, prev_ts
                ));
            }
        }
        self.last_timestamp_ms = Some(timestamp_ms);

        while let Some((old_ts, _)) = self.peaks.front() {
            if timestamp_ms.saturating_sub(*old_ts) > lookback_ms {
                self.peaks.pop_front();
            } else {
                break;
            }
        }
        while let Some((_, peak_equity)) = self.peaks.back() {
            if *peak_equity <= equity {
                self.peaks.pop_back();
            } else {
                break;
            }
        }
        self.peaks.push_back((timestamp_ms, equity));
        Ok(self
            .peaks
            .front()
            .map(|(_, equity)| *equity)
            .unwrap_or(equity))
    }
}

#[allow(dead_code)]
pub fn step(
    state: &mut HardStopState,
    config: HardStopConfig,
    equity: f64,
    timestamp_ms: u64,
) -> Result<HardStopStep, String> {
    let peak = if state.initialized {
        state.peak_strategy_equity.max(equity)
    } else {
        equity
    };
    step_with_peak_strategy_equity(state, config, equity, peak, timestamp_ms)
}

pub fn step_with_peak_strategy_equity(
    state: &mut HardStopState,
    config: HardStopConfig,
    equity: f64,
    peak_strategy_equity: f64,
    timestamp_ms: u64,
) -> Result<HardStopStep, String> {
    config.validate()?;
    if !equity.is_finite() || equity <= 0.0 {
        return Err("equity must be finite and > 0".to_string());
    }
    if !peak_strategy_equity.is_finite() || peak_strategy_equity <= 0.0 {
        return Err("peak_strategy_equity must be finite and > 0".to_string());
    }
    if peak_strategy_equity + f64::EPSILON < equity {
        return Err("peak_strategy_equity must be >= equity".to_string());
    }

    let alpha = 2.0 / (config.ema_span_minutes + 1.0);
    if !alpha.is_finite() || !(0.0 < alpha && alpha <= 1.0) {
        return Err("computed alpha is invalid".to_string());
    }

    let current_minute = timestamp_ms / 60_000;
    let prev_tier = state.tier;
    if !state.initialized {
        state.initialized = true;
        state.peak_strategy_equity = peak_strategy_equity;
        state.drawdown_ema = 0.0;
        state.last_minute = Some(current_minute);
        state.tier = if state.red_latched {
            HardStopTier::Red
        } else {
            HardStopTier::Green
        };
        let step = HardStopStep {
            drawdown_raw: 0.0,
            drawdown_score: 0.0,
            tier: state.tier,
            changed: state.tier != prev_tier,
            alpha,
            elapsed_minutes: 0,
        };
        state.cached_step = Some(step);
        return Ok(step);
    }

    let last_minute = state
        .last_minute
        .ok_or_else(|| "initialized hard-stop state missing last_minute".to_string())?;
    if current_minute < last_minute {
        return Err(format!(
            "timestamp minute must be non-decreasing, got {} after {}",
            current_minute, last_minute
        ));
    }
    let elapsed_minutes = current_minute - last_minute;
    if elapsed_minutes == 0 {
        let mut step = state
            .cached_step
            .ok_or_else(|| "initialized hard-stop state missing cached_step".to_string())?;
        step.changed = false;
        step.elapsed_minutes = 0;
        return Ok(step);
    }

    state.peak_strategy_equity = peak_strategy_equity;
    let drawdown_raw = (1.0 - (equity / peak_strategy_equity.max(f64::EPSILON))).max(0.0);
    let decay = (1.0 - alpha).powf(elapsed_minutes as f64);
    state.drawdown_ema = drawdown_raw + (state.drawdown_ema - drawdown_raw) * decay;
    let drawdown_score = drawdown_raw.min(state.drawdown_ema);

    let yellow_threshold = config.tier_ratios.yellow * config.red_threshold;
    let orange_threshold = config.tier_ratios.orange * config.red_threshold;
    let next_tier = if state.red_latched {
        HardStopTier::Red
    } else if drawdown_score + 1e-12 >= config.red_threshold {
        HardStopTier::Red
    } else if drawdown_score + 1e-12 >= orange_threshold {
        HardStopTier::Orange
    } else if drawdown_score + 1e-12 >= yellow_threshold {
        HardStopTier::Yellow
    } else {
        HardStopTier::Green
    };
    if next_tier == HardStopTier::Red {
        state.red_latched = true;
    }
    state.tier = if state.red_latched {
        HardStopTier::Red
    } else {
        next_tier
    };
    state.last_minute = Some(current_minute);
    let step = HardStopStep {
        drawdown_raw,
        drawdown_score,
        tier: state.tier,
        changed: state.tier != prev_tier,
        alpha,
        elapsed_minutes,
    };
    state.cached_step = Some(step);
    Ok(step)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tier_boundaries_follow_configurable_ratios() {
        let mut state = HardStopState::default();
        let config = HardStopConfig {
            red_threshold: 0.2,
            ema_span_minutes: 1.0,
            tier_ratios: HardStopTierRatios {
                yellow: 0.4,
                orange: 0.8,
            },
        };
        let _ = step(&mut state, config, 100.0, 60_000).unwrap();
        assert_eq!(
            step(&mut state, config, 92.0, 120_000).unwrap().tier,
            HardStopTier::Yellow
        );
        assert_eq!(
            step(&mut state, config, 84.0, 180_000).unwrap().tier,
            HardStopTier::Orange
        );
        assert_eq!(
            step(&mut state, config, 80.0, 240_000).unwrap().tier,
            HardStopTier::Red
        );
    }

    #[test]
    fn same_minute_recall_returns_cached_step_without_advancing() {
        let mut state = HardStopState::default();
        let config = HardStopConfig {
            red_threshold: 0.2,
            ema_span_minutes: 60.0,
            tier_ratios: HardStopTierRatios::default(),
        };
        let _ = step(&mut state, config, 100.0, 60_000).unwrap();
        let first = step(&mut state, config, 90.0, 120_000).unwrap();
        let ema_after_first = state.drawdown_ema;
        let cached = step(&mut state, config, 80.0, 120_500).unwrap();
        assert_eq!(cached.elapsed_minutes, 0);
        assert!(!cached.changed);
        assert!((cached.drawdown_score - first.drawdown_score).abs() < 1e-12);
        assert!((state.drawdown_ema - ema_after_first).abs() < 1e-12);
    }

    #[test]
    fn red_is_latched_once_triggered() {
        let mut state = HardStopState::default();
        let config = HardStopConfig {
            red_threshold: 0.25,
            ema_span_minutes: 1.0,
            tier_ratios: HardStopTierRatios::default(),
        };
        let _ = step(&mut state, config, 100.0, 60_000).unwrap();
        assert_eq!(
            step(&mut state, config, 60.0, 120_000).unwrap().tier,
            HardStopTier::Red
        );
        assert!(state.red_latched);
        assert_eq!(
            step(&mut state, config, 100.0, 180_000).unwrap().tier,
            HardStopTier::Red
        );
    }

    #[test]
    fn rolling_peak_tracker_enforces_window() {
        let mut tracker = RollingPeakTracker::default();
        assert!((tracker.update(1_000, 100.0, 1_000).unwrap() - 100.0).abs() < 1e-12);
        assert!((tracker.update(1_500, 90.0, 1_000).unwrap() - 100.0).abs() < 1e-12);
        assert!((tracker.update(2_100, 95.0, 1_000).unwrap() - 95.0).abs() < 1e-12);
    }
}
