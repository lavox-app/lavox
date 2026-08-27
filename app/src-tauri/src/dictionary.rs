//! Personal dictation dictionary: reader side.
//!
//! The server owns all writes (~/Lavox/dictionary.json, see the server's
//! dictionary.py). The Hub only reads it: the terms bias the whisper decoder
//! via initial_prompt, and the learned (misheard → term) pairs run as a
//! deterministic replacement layer on the transcript before insertion.

use serde::Deserialize;
use std::path::PathBuf;

#[derive(Deserialize, Default)]
pub struct Dictionary {
    #[serde(default)]
    pub terms: Vec<Entry>,
}

#[derive(Deserialize)]
pub struct Entry {
    pub term: String,
    #[serde(default)]
    pub misheard: Vec<String>,
    #[serde(default)]
    pub count: i64,
}

fn dict_path() -> PathBuf {
    if let Ok(p) = std::env::var("LAVOX_DICT_PATH") {
        return PathBuf::from(p);
    }
    PathBuf::from(std::env::var("HOME").unwrap_or_default()).join("Lavox/dictionary.json")
}

pub fn load() -> Dictionary {
    std::fs::read_to_string(dict_path())
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default()
}

impl Dictionary {
    /// Vocabulary-biasing prompt. Whisper weighs the END of the prompt more,
    /// so the most-used terms go last. Budget-capped (the decoder only keeps
    /// 224 prompt tokens).
    pub fn initial_prompt(&self) -> Option<String> {
        if self.terms.is_empty() {
            return None;
        }
        let mut ranked: Vec<&Entry> = self.terms.iter().collect();
        ranked.sort_by_key(|e| e.count);
        let terms: Vec<&str> = ranked
            .iter()
            .rev()
            .take(40)
            .rev() // least-used first, most-used last
            .map(|e| e.term.as_str())
            .collect();
        Some(terms.join(", "))
    }

    /// Deterministic post-ASR layer: replace known mishearings, longest first.
    pub fn apply(&self, text: &str) -> String {
        let mut pairs: Vec<(&str, &str)> = self
            .terms
            .iter()
            .flat_map(|e| e.misheard.iter().map(move |m| (m.as_str(), e.term.as_str())))
            .collect();
        pairs.sort_by_key(|(m, _)| std::cmp::Reverse(m.chars().count()));
        let mut out = text.to_string();
        for (mis, term) in pairs {
            out = replace_word_ci(&out, mis, term);
        }
        out
    }
}

fn is_word_char(c: char) -> bool {
    c.is_alphanumeric()
}

/// Case-insensitive whole-word replacement, accent-safe (char-based).
fn replace_word_ci(text: &str, from: &str, to: &str) -> String {
    let chars: Vec<char> = text.chars().collect();
    let lower: Vec<char> = text.to_lowercase().chars().collect();
    let needle: Vec<char> = from.to_lowercase().chars().collect();
    // to_lowercase can change the char count for exotic scripts, bail out
    // rather than corrupt offsets (the replacement is best-effort anyway).
    if lower.len() != chars.len() || needle.is_empty() {
        return text.to_string();
    }
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    while i < chars.len() {
        let end = i + needle.len();
        let hit = end <= chars.len()
            && lower[i..end] == needle[..]
            && (i == 0 || !is_word_char(chars[i - 1]))
            && (end == chars.len() || !is_word_char(chars[end]));
        if hit {
            out.push_str(to);
            i = end;
        } else {
            out.push(chars[i]);
            i += 1;
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    fn dict(pairs: &[(&str, &[&str])]) -> Dictionary {
        Dictionary {
            terms: pairs
                .iter()
                .map(|(t, mis)| Entry {
                    term: t.to_string(),
                    misheard: mis.iter().map(|m| m.to_string()).collect(),
                    count: 1,
                })
                .collect(),
        }
    }

    #[test]
    fn replaces_whole_words_case_insensitively_with_accents() {
        let d = dict(&[("Lavox", &["lawok"]), ("Müisz", &["müs"])]);
        assert_eq!(
            d.apply("a LAWOK és a müs projektről"),
            "a Lavox és a Müisz projektről"
        );
    }

    #[test]
    fn does_not_touch_substrings_inside_words() {
        let d = dict(&[("Lavox", &["lawok"])]);
        assert_eq!(d.apply("a lawokról nem"), "a lawokról nem");
        assert_eq!(d.apply("lawok."), "Lavox.");
    }

    #[test]
    fn prompt_puts_most_used_terms_last() {
        let mut d = dict(&[("Ritka", &[]), ("Gyakori", &[])]);
        d.terms[1].count = 10;
        assert_eq!(d.initial_prompt().unwrap(), "Ritka, Gyakori");
    }
}
