variable "project_id" {
  description = "Google Cloud project ID that owns the archive resources."
  type        = string

  validation {
    condition     = length(trimspace(var.project_id)) > 0
    error_message = "project_id must not be empty."
  }
}

variable "project_number" {
  description = "Google Cloud project number from the verified archive preflight."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+$", var.project_number))
    error_message = "project_number must contain only decimal digits."
  }
}
