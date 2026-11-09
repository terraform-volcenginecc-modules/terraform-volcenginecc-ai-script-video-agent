terraform {
  required_version = ">= 1.5.7, < 2.0.0"

  required_providers {
    volcenginecc = {
      source  = "volcengine/volcenginecc"
      version = "= 0.0.73"
    }
    time = {
      source  = "hashicorp/time"
      version = "= 0.14.2"
    }
  }
}
