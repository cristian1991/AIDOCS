from pathlib import Path

from aidocs_mcp.schema_index_store import SchemaIndexStore


def test_sync_schema_extracts_csharp_models_and_sql_tables(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "    public int Age;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64),\n"
        "    Status NVARCHAR(32)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    result = store.sync_schema(project_root)
    status = store.schema_status(project_root)
    entities = store.find_schema_entities(project_root, query="Quote", limit=20)
    quote = store.get_schema_entity(project_root, "QuoteDto")
    fields = store.find_schema_field(project_root, "PreferredDoctorId", limit=20)

    assert result["entities"] >= 2
    assert result["fields"] >= 4
    assert status["schema_entities"] >= 2
    assert status["entity_breakdown"]["categories"]["persistence"]["entities"] >= 1
    assert status["entity_breakdown"]["categories"]["data_shapes"]["entities"] >= 1
    assert any(item["entity_name"] == "QuoteDto" for item in entities)
    assert any(item["entity_name"] == "Quotes" for item in entities)
    assert any(field["field_name"] == "PreferredDoctorId" for field in quote["fields"])
    preferred = next(field for field in quote["fields"] if field["field_name"] == "PreferredDoctorId")
    # Falsy flags are now omitted from compact responses — absence means False
    assert preferred.get("required", False) is False
    assert preferred.get("defaulted", False) is False
    assert any(field["entity_name"] in {"QuoteDto", "Quotes"} for field in fields)


def test_schema_query_constructor_returns_record_params(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "AppointmentRequest.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record CreateAppointmentRequest(string PatientId, DateTime StartsAt, string Notes);\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    from aidocs_mcp.code_index_store import CodeIndexStore
    CodeIndexStore().sync_code_files(project_root)
    result = store.get_constructor_params(project_root, "CreateAppointmentRequest")

    assert result["matches"]
    assert result["matches"][0]["params"] == ["string PatientId", "DateTime StartsAt", "string Notes"]


def test_get_entity_properties_returns_lightweight_property_view(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "public class QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "    public decimal LineTotal { get; set; }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_schema(project_root)
    result = store.get_entity_properties(project_root, "QuoteDto")

    assert result["entity_name"] == "QuoteDto"
    assert any(prop["field_name"] == "PreferredDoctorId" for prop in result["properties"])


def test_get_schema_entities_batch_returns_multiple_exact_entities(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "Document.cs").write_text(
        "public class Document { public string Number { get; set; } = string.Empty; }\n"
        "public class DocumentItem { public string Description { get; set; } = string.Empty; }\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_schema(project_root)
    result = store.get_schema_entities_batch(project_root, ["Document", "DocumentItem"])

    names = [item["entity_name"] for item in result["entities"]]
    assert names == ["Document", "DocumentItem"]


def test_trace_entity_flow_combines_schema_and_code_matches(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Models").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Models" / "QuoteDto.cs").write_text(
        "namespace DentalApp.Models;\n\n"
        "public record QuoteDto\n"
        "{\n"
        "    public string PreferredDoctorId { get; set; } = string.Empty;\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Services" / "QuoteService.cs").write_text(
        "using DentalApp.Models;\n\n"
        "public class QuoteService\n"
        "{\n"
        "    public void Handle(QuoteDto dto) { }\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PreferredDoctorId NVARCHAR(64)\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_schema(project_root)
    from aidocs_mcp.code_index_store import CodeIndexStore
    CodeIndexStore().sync_code_files(project_root)
    result = store.trace_entity_flow(project_root, "QuoteDto")

    assert result["entity"] == "QuoteDto"
    assert result["entities"]
    assert result["fields"]
    assert result["code_matches"]


def test_trace_relationship_path_finds_fk_chain(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Db").mkdir(parents=True, exist_ok=True)
    (project_root / "Db" / "schema.sql").write_text(
        "CREATE TABLE Quotes (\n"
        "    Id INT,\n"
        "    PatientId INT\n"
        ");\n"
        "CREATE TABLE QuoteItems (\n"
        "    Id INT,\n"
        "    QuoteId INT,\n"
        "    ServiceId INT\n"
        ");\n"
        "CREATE TABLE Services (\n"
        "    Id INT\n"
        ");\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_schema(project_root)
    result = store.trace_relationship_path(project_root, "QuoteItems", "Services", limit=10)

    assert result["paths"]
    assert result["paths"][0][-1]["target_entity"] == "Services"


def test_sync_schema_extracts_prisma_models_enums_and_relationships(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "prisma").mkdir(parents=True, exist_ok=True)
    (project_root / "prisma" / "schema.prisma").write_text(
        "model User {\n"
        "  id        String   @id\n"
        "  email     String   @unique\n"
        "  purchases Purchase[]\n"
        "}\n\n"
        "model Purchase {\n"
        "  id      String @id\n"
        "  userId  String\n"
        "  user    User   @relation(fields: [userId], references: [id])\n"
        "  status  PurchaseStatus\n"
        "}\n\n"
        "enum PurchaseStatus {\n"
        "  PENDING\n"
        "  COMPLETED\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    result = store.sync_schema(project_root)
    entities = store.find_schema_entities(project_root, query="Purchase", limit=20)
    purchase = store.get_schema_entity(project_root, "Purchase")
    relationships = store.trace_relationship_path(project_root, "Purchase", "User", limit=10)

    assert result["entities"] == 3
    assert result["fields"] >= 7
    assert any(item["entity_name"] == "Purchase" and item["source_type"] == "prisma_model" for item in entities)
    assert any(field["field_name"] == "status" and field["field_kind"] == "enum_field" for field in purchase["fields"])
    assert any(field["field_name"] == "user" and field["field_kind"] == "relation" for field in purchase["fields"])
    assert relationships["paths"]
    assert relationships["paths"][0][-1]["target_entity"] == "User"


def test_sync_schema_extracts_quoted_postgres_migration_sql(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "prisma" / "migrations" / "001").mkdir(parents=True, exist_ok=True)
    (project_root / "prisma" / "migrations" / "001" / "migration.sql").write_text(
        "CREATE TYPE \"UserRole\" AS ENUM ('USER', 'ADMIN');\n\n"
        "CREATE TABLE \"User\" (\n"
        "    \"id\" TEXT NOT NULL,\n"
        "    \"email\" TEXT NOT NULL,\n"
        "    \"role\" \"UserRole\" NOT NULL DEFAULT 'USER',\n"
        "    CONSTRAINT \"User_pkey\" PRIMARY KEY (\"id\")\n"
        ");\n\n"
        "CREATE TABLE \"Account\" (\n"
        "    \"id\" TEXT NOT NULL,\n"
        "    \"userId\" TEXT NOT NULL,\n"
        "    CONSTRAINT \"Account_pkey\" PRIMARY KEY (\"id\")\n"
        ");\n\n"
        "ALTER TABLE \"Account\" ADD CONSTRAINT \"Account_userId_fkey\" FOREIGN KEY (\"userId\") REFERENCES \"User\"(\"id\") ON DELETE CASCADE ON UPDATE CASCADE;\n",
        encoding="utf-8",
    )
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    result = store.sync_schema(project_root)
    entities = store.find_schema_entities(project_root, query="User", limit=20)
    user = store.get_schema_entity(project_root, "User")
    relationships = store.trace_relationship_path(project_root, "Account", "User", limit=10)

    assert result["entities"] == 3
    assert any(item["entity_name"] == "UserRole" and item["kind"] == "enum" for item in entities)
    assert any(field["field_name"] == "role" for field in user["fields"])
    assert relationships["paths"]
    assert relationships["paths"][0][-1]["relation_kind"] == "foreign_key"


def test_schema_status_groups_support_categories_for_csharp_noise(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Pages").mkdir(parents=True, exist_ok=True)
    (project_root / "Services").mkdir(parents=True, exist_ok=True)
    (project_root / "Configurations").mkdir(parents=True, exist_ok=True)
    (project_root / "Domain" / "Entities").mkdir(parents=True, exist_ok=True)
    (project_root / "Pages" / "AccessDenied.cshtml.cs").write_text("public class AccessDeniedModel {}\n", encoding="utf-8")
    (project_root / "Services" / "AccountService.cs").write_text("public class AccountService {}\n", encoding="utf-8")
    (project_root / "Configurations" / "AccountConfiguration.cs").write_text("public class AccountConfiguration {}\n", encoding="utf-8")
    (project_root / "Domain" / "Entities" / "AccountEntity.cs").write_text("public class AccountEntity {}\n", encoding="utf-8")
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)

    store.sync_schema(project_root)
    status = store.schema_status(project_root)

    assert status["entity_breakdown"]["by_source_type"]["csharp_page_model"] == 1
    assert status["entity_breakdown"]["by_source_type"]["csharp_service"] == 1
    assert status["entity_breakdown"]["by_source_type"]["csharp_ef_config"] == 1
    assert status["entity_breakdown"]["by_source_type"]["csharp_entity"] == 1
    assert status["entity_breakdown"]["categories"]["presentation"]["entities"] == 1
    assert status["entity_breakdown"]["categories"]["logic"]["entities"] == 1
    assert status["entity_breakdown"]["categories"]["infrastructure"]["entities"] == 1
    assert status["entity_breakdown"]["categories"]["data_shapes"]["entities"] == 1


def test_sync_schema_extracts_ef_core_tables_and_relationships(tmp_path: Path) -> None:
    store = SchemaIndexStore()
    project_root = tmp_path / "project"
    (project_root / "Infrastructure" / "Data" / "Configurations").mkdir(parents=True, exist_ok=True)
    (project_root / ".MEMORY").mkdir(parents=True, exist_ok=True)
    (project_root / "Infrastructure" / "Data" / "AppDbContext.cs").write_text(
        "using Microsoft.EntityFrameworkCore;\n"
        "public class AppDbContext : DbContext {\n"
        "  public DbSet<Account> Accounts => Set<Account>();\n"
        "  public DbSet<User> Users => Set<User>();\n"
        "}\n",
        encoding="utf-8",
    )
    (project_root / "Infrastructure" / "Data" / "Configurations" / "AccountConfiguration.cs").write_text(
        "using Microsoft.EntityFrameworkCore;\n"
        "using Microsoft.EntityFrameworkCore.Metadata.Builders;\n"
        "public class AccountConfiguration : IEntityTypeConfiguration<Account> {\n"
        "  public void Configure(EntityTypeBuilder<Account> builder) {\n"
        "    builder.ToTable(\"Accounts\");\n"
        "    builder.Property(e => e.Name);\n"
        "    builder.HasOne(e => e.User).WithMany().HasForeignKey(e => e.UserId);\n"
        "  }\n"
        "}\n"
        "public class UserConfiguration : IEntityTypeConfiguration<User> {\n"
        "  public void Configure(EntityTypeBuilder<User> builder) {\n"
        "    builder.ToTable(\"Users\");\n"
        "    builder.Property(e => e.Email);\n"
        "  }\n"
        "}\n",
        encoding="utf-8",
    )

    result = store.sync_schema(project_root)
    status = store.schema_status(project_root)
    entities = store.find_schema_entities(project_root, query="Account", limit=20)
    relationship_paths = store.trace_relationship_path(project_root, "Accounts", "Users", limit=10)

    assert result["entities"] >= 2
    assert any(item["entity_name"] == "Accounts" and item["source_type"] == "ef_table" for item in entities)
    assert status["entity_breakdown"]["by_source_type"]["ef_table"] == 2
    assert status["entity_breakdown"]["categories"]["persistence"]["entities"] >= 2
    assert relationship_paths["paths"]
    assert relationship_paths["paths"][0][-1]["target_entity"] == "Users"
