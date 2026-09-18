use std::collections::{HashMap, HashSet};
use std::io::{self, Read};

#[derive(Debug)]
struct Reservation {
    tenant: String,
    id: String,
    quantity: i32,
    status: String,
}

#[derive(Debug)]
struct Job {
    tenant: String,
    reservation_id: String,
    delivery_key: String,
    state: String,
}

fn json_escape(value: &str) -> String {
    let mut result = String::new();
    for character in value.chars() {
        match character {
            '"' => result.push_str("\\\""),
            '\\' => result.push_str("\\\\"),
            '\n' => result.push_str("\\n"),
            '\r' => result.push_str("\\r"),
            '\t' => result.push_str("\\t"),
            character if character.is_control() => {
                result.push_str(&format!("\\u{:04x}", character as u32))
            }
            character => result.push(character),
        }
    }
    result
}

fn parse_quantity(value: &str) -> Result<i32, String> {
    if value.is_empty() || !value.bytes().all(|byte| byte.is_ascii_digit()) {
        return Err("quantity must be an integer".to_string());
    }
    let quantity = value
        .parse::<i32>()
        .map_err(|_| "quantity is out of range".to_string())?;
    if !(1..=8).contains(&quantity) {
        return Err("quantity must be between 1 and 8".to_string());
    }
    Ok(quantity)
}

fn nonempty(value: &str, field: &str) -> Result<String, String> {
    if value.is_empty() {
        Err(format!("{} must be nonempty", field))
    } else {
        Ok(value.to_string())
    }
}

fn parse(input: &str) -> (Vec<Reservation>, Vec<Job>, Vec<String>) {
    let mut reservations = Vec::new();
    let mut jobs = Vec::new();
    let mut violations = Vec::new();
    for (line_number, line) in input.lines().enumerate() {
        let line_number = line_number + 1;
        let fields: Vec<&str> = line.split('\t').collect();
        let error = if fields.len() != 5 {
            Some("expected exactly five tab-separated fields".to_string())
        } else if fields[0] == "reservation" {
            let parsed = (|| {
                let tenant = nonempty(fields[1], "tenant")?;
                let id = nonempty(fields[2], "reservation id")?;
                let quantity = parse_quantity(fields[3])?;
                if !matches!(fields[4], "reserved" | "approved" | "cancelled" | "fulfilled") {
                    return Err("invalid reservation status".to_string());
                }
                reservations.push(Reservation {
                    tenant,
                    id,
                    quantity,
                    status: fields[4].to_string(),
                });
                Ok(())
            })();
            parsed.err()
        } else if fields[0] == "job" {
            let parsed = (|| {
                let tenant = nonempty(fields[1], "tenant")?;
                let reservation_id = nonempty(fields[2], "reservation id")?;
                let delivery_key = nonempty(fields[3], "delivery key")?;
                if !matches!(fields[4], "pending" | "done") {
                    return Err("invalid job state".to_string());
                }
                jobs.push(Job {
                    tenant,
                    reservation_id,
                    delivery_key,
                    state: fields[4].to_string(),
                });
                Ok(())
            })();
            parsed.err()
        } else {
            Some("record type must be reservation or job".to_string())
        };
        if let Some(message) = error {
            violations.push(format!("line {}: {}", line_number, message));
        }
    }
    (reservations, jobs, violations)
}

fn audit(reservations: &[Reservation], jobs: &[Job], mut violations: Vec<String>) -> Vec<String> {
    let mut reservation_ids = HashSet::new();
    let mut delivery_keys = HashSet::new();
    let mut job_reservation_ids = HashSet::new();
    let mut by_id: HashMap<&str, &Reservation> = HashMap::new();
    let mut capacity: HashMap<&str, i32> = HashMap::new();

    for reservation in reservations {
        if !reservation_ids.insert(reservation.id.as_str()) {
            violations.push(format!("duplicate reservation id: {}", reservation.id));
        }
        by_id.insert(reservation.id.as_str(), reservation);
        if matches!(reservation.status.as_str(), "reserved" | "approved" | "fulfilled") {
            *capacity.entry(reservation.tenant.as_str()).or_insert(0) += reservation.quantity;
        }
    }
    for (tenant, quantity) in capacity {
        if quantity > 8 {
            violations.push(format!("tenant {} exceeds active capacity: {}", tenant, quantity));
        }
    }

    for job in jobs {
        if !delivery_keys.insert(job.delivery_key.as_str()) {
            violations.push(format!("duplicate delivery key: {}", job.delivery_key));
        }
        if !job_reservation_ids.insert(job.reservation_id.as_str()) {
            violations.push(format!("duplicate job for reservation: {}", job.reservation_id));
        }
        match by_id.get(job.reservation_id.as_str()) {
            None => violations.push(format!("job references unknown reservation: {}", job.reservation_id)),
            Some(reservation) => {
                if reservation.tenant != job.tenant {
                    violations.push(format!("job tenant mismatch for reservation: {}", job.reservation_id));
                }
                if matches!(reservation.status.as_str(), "reserved" | "cancelled") {
                    violations.push(format!("job exists for {} reservation: {}", reservation.status, job.reservation_id));
                }
                if reservation.status == "fulfilled" && job.state != "done" {
                    violations.push(format!("fulfilled reservation job is not done: {}", job.reservation_id));
                }
            }
        }
    }
    for reservation in reservations.iter().filter(|item| matches!(item.status.as_str(), "approved" | "fulfilled")) {
        let matching: Vec<&Job> = jobs
            .iter()
            .filter(|job| job.reservation_id == reservation.id && job.tenant == reservation.tenant)
            .collect();
        if matching.len() != 1 {
            violations.push(format!("{} reservation must have exactly one matching job: {}", reservation.status, reservation.id));
        }
    }
    violations
}

fn main() -> std::process::ExitCode {
    let mut input = String::new();
    if let Err(error) = io::stdin().read_to_string(&mut input) {
        eprintln!("could not read stdin: {}", error);
        return std::process::ExitCode::from(2);
    }
    let (reservations, jobs, parse_violations) = parse(&input);
    let violations = audit(&reservations, &jobs, parse_violations);
    let rendered = violations
        .iter()
        .map(|violation| format!("\"{}\"", json_escape(violation)))
        .collect::<Vec<String>>()
        .join(",");
    println!(
        "{{\"passed\":{},\"violations\":[{}],\"reservations\":{},\"jobs\":{}}}",
        violations.is_empty(),
        rendered,
        reservations.len(),
        jobs.len()
    );
    if violations.iter().any(|violation| violation.starts_with("line ")) {
        std::process::ExitCode::from(2)
    } else if violations.is_empty() {
        std::process::ExitCode::SUCCESS
    } else {
        std::process::ExitCode::from(1)
    }
}
