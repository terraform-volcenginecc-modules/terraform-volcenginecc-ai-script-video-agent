terraform {
  required_version = ">= 1.5.7, < 2.0.0"

  required_providers {
    volcenginecc = {
      source  = "volcengine/volcenginecc"
      version = "= 0.0.73"
    }
  }
}

provider "volcenginecc" {
  region = var.region
  # Credentials are resolved from the provider environment / credential chain.
}
