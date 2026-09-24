terraform {
  required_version = ">= 1.7.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  resource_provider_registrations = "none"
}

variable "suffix" {
  type    = string
  default = "2304184"
}

resource "azurerm_resource_group" "demo" {
  name     = "sit722-103hd-rg"
  location = "eastus"
  tags     = { Task = "SIT722-10.3HD", Purpose = "temporary-rollback-demo" }
}

resource "azurerm_container_registry" "demo" {
  name                = "sit722103hd${var.suffix}"
  resource_group_name = azurerm_resource_group.demo.name
  location            = azurerm_resource_group.demo.location
  sku                 = "Basic"
  admin_enabled       = false
}

resource "azurerm_kubernetes_cluster" "demo" {
  name                = "sit722-103hd-aks"
  resource_group_name = azurerm_resource_group.demo.name
  location            = azurerm_resource_group.demo.location
  dns_prefix          = "sit722-103hd-${var.suffix}"
  sku_tier            = "Free"
  default_node_pool {
    name       = "demo"
    node_count = 1
    vm_size    = "Standard_DS2_v2"
  }
  identity { type = "SystemAssigned" }
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
  }
}

resource "azurerm_role_assignment" "pull" {
  scope                            = azurerm_container_registry.demo.id
  principal_id                     = azurerm_kubernetes_cluster.demo.kubelet_identity[0].object_id
  role_definition_name             = "AcrPull"
  skip_service_principal_aad_check = true
}

# Scope the existing GitHub OIDC identity to this disposable demo group.
resource "azurerm_role_assignment" "github_demo" {
  scope                = azurerm_resource_group.demo.id
  principal_id         = "e7d44efa-36f1-405c-b474-837fb577699a"
  role_definition_name = "Contributor"
}

output "registry" { value = azurerm_container_registry.demo.login_server }
output "cluster" { value = azurerm_kubernetes_cluster.demo.name }
output "resource_group" { value = azurerm_resource_group.demo.name }
