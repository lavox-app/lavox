//! Microphone recorder — both timed (M1) and streaming start/stop (M4).

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// Names of available input (microphone) devices.
pub fn list_input_devices() -> Result<Vec<String>, String> {
    let host = cpal::default_host();
    let devices = host.input_devices().map_err(|e| e.to_string())?;
    let mut names = Vec::new();
    for d in devices {
        if let Ok(name) = d.name() {
            names.push(name);
        }
    }
    Ok(names)
}

/// Record the default input device for `seconds` and write a 16-bit mono-or-multi
/// WAV to `path`. Returns once recording + encoding finishes.
pub fn record_mic_to_wav(path: &str, seconds: u32) -> Result<(), String> {
    let host = cpal::default_host();
    let device = host
        .default_input_device()
        .ok_or_else(|| "no default input device".to_string())?;
    let supported = device.default_input_config().map_err(|e| e.to_string())?;

    let sample_rate = supported.sample_rate().0;
    let channels = supported.channels();
    let sample_format = supported.sample_format();
    let config: cpal::StreamConfig = supported.into();

    let buffer = Arc::new(Mutex::new(Vec::<f32>::new()));
    let buf_cb = buffer.clone();
    let err_fn = |err| eprintln!("input stream error: {err}");

    let stream = match sample_format {
        cpal::SampleFormat::F32 => device.build_input_stream(
            &config,
            move |data: &[f32], _: &cpal::InputCallbackInfo| {
                if let Ok(mut b) = buf_cb.lock() {
                    b.extend_from_slice(data);
                }
            },
            err_fn,
            None,
        ),
        cpal::SampleFormat::I16 => device.build_input_stream(
            &config,
            move |data: &[i16], _: &cpal::InputCallbackInfo| {
                if let Ok(mut b) = buf_cb.lock() {
                    b.extend(data.iter().map(|&s| s as f32 / i16::MAX as f32));
                }
            },
            err_fn,
            None,
        ),
        cpal::SampleFormat::U16 => device.build_input_stream(
            &config,
            move |data: &[u16], _: &cpal::InputCallbackInfo| {
                if let Ok(mut b) = buf_cb.lock() {
                    b.extend(data.iter().map(|&s| (s as f32 / u16::MAX as f32) * 2.0 - 1.0));
                }
            },
            err_fn,
            None,
        ),
        other => return Err(format!("unsupported sample format: {other:?}")),
    }
    .map_err(|e| e.to_string())?;

    stream.play().map_err(|e| e.to_string())?;
    std::thread::sleep(Duration::from_secs(seconds.max(1) as u64));
    drop(stream);

    let spec = hound::WavSpec {
        channels,
        sample_rate,
        bits_per_sample: 16,
        sample_format: hound::SampleFormat::Int,
    };
    let mut writer = hound::WavWriter::create(path, spec).map_err(|e| e.to_string())?;
    let b = buffer.lock().map_err(|e| e.to_string())?;
    for &s in b.iter() {
        let v = (s.clamp(-1.0, 1.0) * i16::MAX as f32) as i16;
        writer.write_sample(v).map_err(|e| e.to_string())?;
    }
    writer.finalize().map_err(|e| e.to_string())?;
    Ok(())
}

pub struct StreamingRecorder {
    stop_flag: Arc<AtomicBool>,
    buffer: Arc<Mutex<Vec<f32>>>,
    sample_rate: u32,
    channels: u16,
    /// Pillanatnyi mikrofon-szint (RMS) f32::to_bits formában — a valós idejű
    /// waveformhoz. A felvevő callback frissíti minden audio-chunknál.
    level: Arc<AtomicU32>,
    thread: Option<std::thread::JoinHandle<()>>,
}

impl StreamingRecorder {
    /// Felvétel indítása. Ha `mic_name` = Some(név), azt a bemeneti eszközt
    /// használja (a bar mic-választója adja); egyébként az alapértelmezettet.
    pub fn start(mic_name: Option<String>) -> Result<Self, String> {
        let host = cpal::default_host();
        let device = match mic_name.as_deref() {
            Some(name) if !name.is_empty() => host
                .input_devices()
                .ok()
                .and_then(|mut ds| ds.find(|d| d.name().map(|n| n == name).unwrap_or(false)))
                .or_else(|| host.default_input_device())
                .ok_or_else(|| "Nincs mikrofon".to_string())?,
            _ => host
                .default_input_device()
                .ok_or_else(|| "Nincs mikrofon".to_string())?,
        };
        let supported = device.default_input_config().map_err(|e| e.to_string())?;

        let sample_rate = supported.sample_rate().0;
        let channels = supported.channels();
        let config: cpal::StreamConfig = supported.into();

        let buffer = Arc::new(Mutex::new(Vec::<f32>::new()));
        let buf_cb = buffer.clone();
        let stop_flag = Arc::new(AtomicBool::new(false));
        let stop_clone = stop_flag.clone();
        let level = Arc::new(AtomicU32::new(0));
        let level_cb = level.clone();

        let thread = std::thread::spawn(move || {
            let stream = device
                .build_input_stream(
                    &config,
                    move |data: &[f32], _: &cpal::InputCallbackInfo| {
                        if let Ok(mut b) = buf_cb.lock() {
                            b.extend_from_slice(data);
                        }
                        // Pillanatnyi RMS szint a valós idejű waveformhoz.
                        if !data.is_empty() {
                            let sum: f32 = data.iter().map(|s| s * s).sum();
                            let rms = (sum / data.len() as f32).sqrt();
                            level_cb.store(rms.to_bits(), Ordering::Relaxed);
                        }
                    },
                    |err| eprintln!("stream error: {err}"),
                    None,
                )
                .expect("failed to build input stream");
            stream.play().expect("failed to start stream");

            while !stop_clone.load(Ordering::Relaxed) {
                std::thread::sleep(Duration::from_millis(100));
            }
            drop(stream);
        });

        Ok(Self {
            stop_flag,
            buffer,
            sample_rate,
            channels,
            level,
            thread: Some(thread),
        })
    }

    /// Megosztott szint-mérő handle a valós idejű waveformhoz (RMS, 0.0–~1.0).
    pub fn level_handle(&self) -> Arc<AtomicU32> {
        self.level.clone()
    }

    pub fn stop_and_save(mut self, path: &str) -> Result<String, String> {
        self.stop_flag.store(true, Ordering::Relaxed);
        if let Some(handle) = self.thread.take() {
            let _ = handle.join();
        }

        let spec = hound::WavSpec {
            channels: self.channels,
            sample_rate: self.sample_rate,
            bits_per_sample: 16,
            sample_format: hound::SampleFormat::Int,
        };
        let mut writer = hound::WavWriter::create(path, spec).map_err(|e| e.to_string())?;
        let b = self.buffer.lock().map_err(|e| e.to_string())?;
        for &s in b.iter() {
            let v = (s.clamp(-1.0, 1.0) * i16::MAX as f32) as i16;
            writer.write_sample(v).map_err(|e| e.to_string())?;
        }
        writer.finalize().map_err(|e| e.to_string())?;
        Ok(path.to_string())
    }
}
