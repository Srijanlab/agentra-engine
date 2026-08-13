output "pubsub_topic" {
  value = google_pubsub_topic.work_queue.id
}

output "alarm_webhook_password_secret" {
  value = google_secret_manager_secret.alarm_webhook_password.secret_id
}
